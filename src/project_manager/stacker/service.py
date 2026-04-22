from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote

from .db import StackerDB
from .models import OperationState, ParentLocator, RepoPRConfig, SelectorTarget, TrackedBranch
from . import gh, git, selectors


class StackerService:
    def __init__(self, db: StackerDB, *, progress: Callable[[str], None] | None = None) -> None:
        self.db = db
        self.progress = progress

    def create(self, branch_ref: str, create_branch: bool, base_branch: str | None) -> str:
        current = self._current_context_or_none()
        target_repo_root = self._target_repo_for_create(branch_ref, current.repo_root if current else None)
        if base_branch is None:
            if current is None:
                raise git.GitError("Without -B/--base, stacker create must be run from the parent worktree.")
            if current.repo_root != target_repo_root:
                raise git.GitError("Without -B/--base, stacker create must be run from the parent worktree in the same repo.")
            parent = ParentLocator(
                repo_root=current.repo_root,
                worktree_path=current.worktree_path,
                branch=current.branch,
            )
        else:
            parent = selectors.resolve_parent_for_base(target_repo_root, base_branch, current)

        result_file = Path(os.environ.get("TMPDIR", "/tmp")) / f"stacker-create-{os.getpid()}.txt"
        if result_file.exists():
            result_file.unlink()
        cmd = ["wt", "create", "--result-file", str(result_file)]
        if create_branch:
            cmd.append("-b")
        if base_branch is not None:
            cmd.extend(["-B", parent.branch])
        cmd.append(branch_ref)
        proc = subprocess.run(cmd, text=True, stdout=sys.stderr, stderr=sys.stderr)
        if proc.returncode != 0:
            raise git.GitError(f"'{' '.join(cmd)}' failed with exit code {proc.returncode}.")
        worktree_path = result_file.read_text().strip()
        result_file.unlink(missing_ok=True)
        branch = git.current_branch(worktree_path)
        parent_head = git.rev_parse(parent.worktree_path, parent.branch)
        self.db.upsert_worktree(
            TrackedBranch(
                repo_root=target_repo_root,
                worktree_path=worktree_path,
                branch=branch,
                parent_repo_root=parent.repo_root,
                parent_worktree_path=parent.worktree_path,
                parent_branch=parent.branch,
                managed_base_commit=parent_head,
                last_synced_parent_commit=parent_head,
                last_clean_head=git.rev_parse(worktree_path, "HEAD"),
            )
        )
        return worktree_path

    def track(self, target: SelectorTarget, parent: ParentLocator) -> TrackedBranch:
        if target.repo_root != parent.repo_root:
            raise git.GitError("Parent and child must be in the same repo.")
        base_commit = git.merge_base(target.repo_root, parent.branch, target.branch)
        tracked = TrackedBranch(
            repo_root=target.repo_root,
            worktree_path=target.worktree_path,
            branch=target.branch,
            parent_repo_root=parent.repo_root,
            parent_worktree_path=parent.worktree_path,
            parent_branch=parent.branch,
            managed_base_commit=base_commit,
            last_synced_parent_commit=git.rev_parse(parent.worktree_path, parent.branch),
            last_clean_head=git.rev_parse(target.worktree_path, "HEAD"),
        )
        self.db.upsert_worktree(tracked)
        return tracked

    def resolve_untrack_target(self, selector: str | None) -> SelectorTarget:
        try:
            return selectors.resolve_target(selector, allow_branch_without_worktree=False)
        except git.GitError as original_error:
            if selector is None:
                raise
            parsed = selectors.parse_selector(selector)
            if parsed.is_root or not parsed.branch:
                raise
            current = self._current_context_or_none()
            repo_root = current.repo_root if parsed.repo_query is None and current else None
            if parsed.repo_query is not None:
                repo_root = selectors.resolve_repo_path(parsed.repo_query, current.repo_root if current else None)
            if repo_root is None:
                raise original_error
            tracked = self.db.get_worktree_by_branch(repo_root, parsed.branch)
            if tracked:
                return SelectorTarget(
                    repo_root=tracked.repo_root,
                    worktree_path=tracked.worktree_path,
                    branch=tracked.branch,
                )
            raise original_error

    def guard_no_rebase(self) -> None:
        current = git.current_context()
        tracked = self.db.get_worktree_by_branch(current.repo_root, current.branch)
        if not tracked:
            return
        if self.db.get_operation(current.repo_root):
            raise git.GitError(
                f"{selectors.selector_for(current.repo_root, current.branch)} is managed by stacker and "
                "has an active stacker operation. Do not rebase it; use 'stacker continue' or 'stacker abort'."
            )
        raise git.GitError(
            f"{selectors.selector_for(current.repo_root, current.branch)} is managed by stacker. "
            "Do not rebase it; use stacker sync/push/repair instead."
        )

    def complete_untrack_targets(self, prefix: str, current_repo_root: str | None) -> list[str]:
        parsed = selectors.parse_selector(prefix) if prefix else selectors.ParsedSelector(None, None, False)
        if parsed.repo_query is None and current_repo_root:
            items = self.db.list_worktrees(current_repo_root)
            values = [item.branch for item in items]
        else:
            items = self.db.list_worktrees()
            values = [f"{selectors.repo_display_name(item.repo_root)}:{item.branch}" for item in items]
        seen: set[str] = set()
        matches: list[str] = []
        for value in values:
            if not value.startswith(prefix):
                continue
            if value in seen:
                continue
            seen.add(value)
            matches.append(value)
        matches.sort()
        return matches

    def untrack(self, target: SelectorTarget) -> str:
        existing = self.db.get_operation(target.repo_root)
        if existing:
            raise git.GitError("Another stacker operation is active for this repo. Use 'stacker continue' or 'stacker abort'.")
        tracked = self.db.get_worktree(target.repo_root, target.worktree_path, target.branch)
        if not tracked:
            raise git.GitError(
                f"{selectors.selector_for(target.repo_root, target.branch)} is not tracked."
            )
        for child in self.db.get_children(tracked.repo_root, tracked.worktree_path, tracked.branch):
            self.db.upsert_worktree(
                TrackedBranch(
                    repo_root=child.repo_root,
                    worktree_path=child.worktree_path,
                    branch=child.branch,
                    parent_repo_root=tracked.parent_repo_root,
                    parent_worktree_path=tracked.parent_worktree_path,
                    parent_branch=tracked.parent_branch,
                    managed_base_commit=child.managed_base_commit,
                    last_synced_parent_commit=child.last_synced_parent_commit,
                    last_clean_head=child.last_clean_head,
                )
            )
        deleted = self.db.delete_worktree(target.repo_root, target.worktree_path, target.branch)
        if deleted == 0:
            raise git.GitError(f"{selectors.selector_for(target.repo_root, target.branch)} is not tracked.")
        return f"Untracked {selectors.selector_for(target.repo_root, target.branch)}"

    def delete(self, target: SelectorTarget, *, delete_branch: bool = False) -> str:
        existing = self.db.get_operation(target.repo_root)
        if existing:
            raise git.GitError("Another stacker operation is active for this repo. Use 'stacker continue' or 'stacker abort'.")
        selector = selectors.selector_for(target.repo_root, target.branch)
        cmd = ["wt", "delete"]
        if delete_branch:
            cmd.append("-b")
        cmd.append(selector)
        proc = subprocess.run(cmd, text=True, stdout=sys.stderr, stderr=sys.stderr)
        if proc.returncode != 0:
            raise git.GitError(f"'{' '.join(cmd)}' failed with exit code {proc.returncode}.")
        tracked = self.db.get_worktree(target.repo_root, target.worktree_path, target.branch)
        if tracked:
            self.untrack(target)
            return f"Deleted and untracked {selector}"
        return f"Deleted {selector}"

    def status_text(self, target: SelectorTarget) -> str:
        tracked = self.db.get_worktree(target.repo_root, target.worktree_path, target.branch)
        op = self.db.get_operation(target.repo_root)
        lines = [f"{selectors.selector_for(target.repo_root, target.branch)}", f"path: {target.worktree_path}"]
        if tracked:
            lines.extend(
                [
                    f"parent: {selectors.selector_for(tracked.parent_repo_root, tracked.parent_branch)}",
                    f"parent path: {tracked.parent_worktree_path}",
                    f"managed base: {tracked.managed_base_commit}",
                    f"last synced parent: {tracked.last_synced_parent_commit or '-'}",
                    f"last clean head: {tracked.last_clean_head or '-'}",
                ]
            )
        else:
            children = self.db.get_children(target.repo_root, target.worktree_path, target.branch)
            if children:
                lines.append(f"untracked root with {len(children)} tracked child(ren)")
            else:
                lines.append("not tracked")
        if op:
            lines.append(f"operation: {op.op_type} ({op.status})")
            if op.worktree_path:
                lines.append(f"active worktree: {op.worktree_path}")
            if op.error_message:
                lines.append(f"last error: {op.error_message}")
        else:
            lines.append("operation: none")
        return "\n".join(lines)

    def log_text(self, target: SelectorTarget) -> str:
        tracked = self._require_tracked(target)
        entries = git.log_subject_and_author(
            tracked.worktree_path,
            f"{tracked.managed_base_commit}..HEAD",
        )
        if not entries:
            return (
                f"No commits since {selectors.selector_for(tracked.parent_repo_root, tracked.parent_branch)} "
                f"at {self._short(tracked.managed_base_commit)}."
            )
        lines = [f"{selectors.selector_for(tracked.repo_root, tracked.branch)} commits since parent:"]
        for subject, author in entries:
            lines.append(f"{subject} ({author})" if author else subject)
        return "\n".join(lines)

    def parent_text(self, target: SelectorTarget) -> str:
        tracked = self._require_tracked(target)
        return tracked.managed_base_commit

    def graph_text(self, repo_root: str | None = None) -> str:
        worktrees = self.db.list_worktrees(repo_root)
        if not worktrees:
            return "No tracked worktrees."
        current = self._current_context_or_none()
        tracked_index = {(item.worktree_path, item.branch): item for item in worktrees}
        by_repo: dict[str, list[TrackedBranch]] = {}
        for item in worktrees:
            by_repo.setdefault(item.repo_root, []).append(item)
        lines: list[str] = []
        for repo, items in sorted(by_repo.items()):
            live_worktrees = {
                (info.path, info.branch): info
                for info in git.worktree_list(repo)
                if info.branch
            }
            header = f"{selectors.repo_display_name(repo)} ({repo})"
            lines.append(self._style(header, fg="blue", bold=True))
            tracked_keys = {(w.worktree_path, w.branch) for w in items}
            roots: list[tuple[str, str, bool]] = []
            for item in items:
                parent_key = (item.parent_worktree_path, item.parent_branch)
                if parent_key not in tracked_keys:
                    roots.append((item.parent_worktree_path, item.parent_branch, True))
            seen_roots: set[tuple[str, str]] = set()
            for path, branch, implicit in sorted(roots, key=lambda it: (it[1], it[0])):
                key = (path, branch)
                if key in seen_roots:
                    continue
                seen_roots.add(key)
                self._render_graph_node(lines, repo, path, branch, items, tracked_index, live_worktrees, "", True, implicit, current)
        return "\n".join(lines)

    def set_pr_mode(self, repo_root: str, mode: str, trunk_branch: str, main_repo: str | None) -> RepoPRConfig:
        if mode not in {"normal", "forked"}:
            raise git.GitError("PR mode must be 'normal' or 'forked'.")
        if mode == "normal" and main_repo:
            raise git.GitError("--main-repo is only valid for forked mode.")
        if mode == "forked" and not main_repo:
            raise git.GitError("--main-repo is required for forked mode.")
        config = RepoPRConfig(
            repo_root=repo_root,
            mode=mode,
            trunk_branch=trunk_branch,
            main_repo=main_repo,
        )
        self.db.upsert_repo_pr_config(config)
        return config

    def show_pr_mode(self, repo_root: str) -> str:
        config = self._pr_config(repo_root)
        lines = [
            f"repo: {selectors.repo_display_name(repo_root)}",
            f"mode: {config.mode}",
            f"trunk: {config.trunk_branch}",
        ]
        if config.main_repo:
            lines.append(f"main repo: {config.main_repo}")
        else:
            lines.append(f"main repo: {self._current_repo_slug(repo_root)}")
        return "\n".join(lines)

    def pr(self, target: SelectorTarget, *, draft: bool) -> str:
        tracked = self._require_tracked(target)
        config = self._pr_config(tracked.repo_root)
        logs: list[str] = []
        current_repo = gh.repo_info(cwd=tracked.repo_root)
        self._run_single_pp(tracked, logs)
        current_pr = self._create_or_update_current_pr(tracked, config, current_repo, draft=draft, logs=logs)
        self._refresh_component_pr_bodies(tracked, config, current_repo, current_pr, logs)
        return self._finish(logs, f"PR ready: {current_pr.url}")

    def sync(self, target: SelectorTarget) -> str:
        tracked = self._require_tracked(target)
        existing = self.db.get_operation(target.repo_root)
        if existing:
            raise git.GitError("Another stacker operation is active for this repo. Use 'stacker continue' or 'stacker abort'.")
        parent_head, _, _ = self._sync_plan(tracked)
        if parent_head == tracked.managed_base_commit:
            return (
                f"Nothing to sync for {selectors.selector_for(tracked.repo_root, tracked.branch)}.\n"
                f"Parent {selectors.selector_for(tracked.parent_repo_root, tracked.parent_branch)} is unchanged at {self._short(parent_head)}."
            )
        self._ensure_syncable(target.worktree_path)
        logs: list[str] = []
        self._prepare_local_operation(tracked, op_type="local_sync", logs=logs)
        return self._run_until_pause_or_finish(target.repo_root, logs=logs)

    def repair(self, target: SelectorTarget, base_ref: str) -> str:
        tracked = self._require_tracked(target)
        if self.db.get_operation(target.repo_root):
            raise git.GitError("Another stacker operation is active for this repo. Use 'stacker continue' or 'stacker abort'.")
        self._ensure_syncable(target.worktree_path)
        actual_base = git.rev_parse(tracked.worktree_path, base_ref)
        current_head = git.rev_parse(tracked.worktree_path, "HEAD")
        if (
            actual_base == tracked.managed_base_commit
            and actual_base == (tracked.last_synced_parent_commit or "")
            and current_head == (tracked.last_clean_head or "")
        ):
            return (
                f"No repair needed for {selectors.selector_for(tracked.repo_root, tracked.branch)}.\n"
                f"Stored base already matches {self._short(actual_base)}."
            )
        repaired = TrackedBranch(
            repo_root=tracked.repo_root,
            worktree_path=tracked.worktree_path,
            branch=tracked.branch,
            parent_repo_root=tracked.parent_repo_root,
            parent_worktree_path=tracked.parent_worktree_path,
            parent_branch=tracked.parent_branch,
            managed_base_commit=actual_base,
            last_synced_parent_commit=actual_base,
            last_clean_head=current_head,
        )
        self.db.upsert_worktree(repaired)
        return (
            f"Repaired {selectors.selector_for(tracked.repo_root, tracked.branch)}.\n"
            f"Stored base ref: {base_ref} -> {self._short(actual_base)}\n"
            f"Managed base: {self._short(tracked.managed_base_commit)} -> {self._short(actual_base)}\n"
            f"Last synced parent: {self._short(tracked.last_synced_parent_commit or '-') if tracked.last_synced_parent_commit else '-'} -> {self._short(actual_base)}"
        )

    def push(self, target: SelectorTarget) -> str:
        if self.db.get_operation(target.repo_root):
            raise git.GitError("Another stacker operation is active for this repo. Use 'stacker continue' or 'stacker abort'.")
        queue = self._toposorted_descendants(target.repo_root, target.worktree_path, target.branch)
        if not queue:
            return "No downstream tracked worktrees to sync."
        for child in queue:
            self._ensure_syncable(child.worktree_path)
        op = OperationState(
            repo_root=target.repo_root,
            op_type="downstream_sync",
            status="running",
            root_path=target.worktree_path,
            root_branch=target.branch,
            queue=[child.worktree_path for child in queue],
            current_index=0,
        )
        self.db.put_operation(op)
        return self._run_until_pause_or_finish(target.repo_root, logs=[])

    def pp(
        self,
        target: SelectorTarget,
        *,
        force_stack_block: bool = False,
        erase_stack_block: bool = False,
    ) -> str:
        logs: list[str] = []
        queue: list[TrackedBranch] = []
        tracked = self.db.get_worktree(target.repo_root, target.worktree_path, target.branch)
        if tracked:
            queue.append(tracked)
        queue.extend(self._toposorted_descendants(target.repo_root, target.worktree_path, target.branch))
        if not queue:
            return "No tracked worktrees to push."

        config = self._pr_config(target.repo_root)
        try:
            current_repo = gh.repo_info(cwd=target.repo_root)
        except git.GitError:
            current_repo = None
        refresh_root: TrackedBranch | None = None
        for item in queue:
            if not self._run_single_pp(item, logs, require_upstream_before=True):
                return self._finish(logs, "PP complete.")
            if current_repo and refresh_root is None and self._find_open_pr(item, config, current_repo):
                refresh_root = item
        should_refresh_block = config.mode == "forked" or force_stack_block or erase_stack_block
        if should_refresh_block and current_repo and refresh_root is not None:
            if erase_stack_block:
                self._erase_component_pr_bodies(refresh_root, config, current_repo, logs)
            else:
                self._refresh_component_pr_bodies(refresh_root, config, current_repo, None, logs)
        return self._finish(logs, "PP complete.")

    def next_path(self, target: SelectorTarget) -> str:
        child = self._only_child(target)
        if not Path(child.worktree_path).exists():
            raise git.GitError(f"Tracked worktree path does not exist: {child.worktree_path}")
        return child.worktree_path

    def next_revision(self, target: SelectorTarget) -> str:
        child = self._only_child(target)
        return child.branch

    def prev_path(self, target: SelectorTarget) -> str:
        tracked = self.db.get_worktree(target.repo_root, target.worktree_path, target.branch)
        if not tracked:
            raise git.GitError(
                f"No tracked parent for {selectors.selector_for(target.repo_root, target.branch)}."
            )
        if not Path(tracked.parent_worktree_path).exists():
            raise git.GitError(f"Tracked parent path does not exist: {tracked.parent_worktree_path}")
        return tracked.parent_worktree_path

    def prev_revision(self, target: SelectorTarget) -> str:
        tracked = self.db.get_worktree(target.repo_root, target.worktree_path, target.branch)
        if not tracked:
            raise git.GitError(
                f"No tracked parent for {selectors.selector_for(target.repo_root, target.branch)}."
            )
        return tracked.parent_branch

    def continue_operation(self, target_repo_root: str | None = None) -> str:
        repo_root = target_repo_root or git.current_context().repo_root
        op = self.db.get_operation(repo_root)
        if not op:
            raise git.GitError("No paused stacker operation for this repo.")
        return self._run_until_pause_or_finish(repo_root, continuing=True, logs=[])

    def abort_operation(self, target_repo_root: str | None = None) -> str:
        repo_root = target_repo_root or git.current_context().repo_root
        op = self.db.get_operation(repo_root)
        if not op:
            raise git.GitError("No active stacker operation for this repo.")
        if op.worktree_path and git.cherry_pick_in_progress(op.worktree_path):
            git.cherry_pick_abort(op.worktree_path)
        if op.worktree_path and op.start_head:
            git.reset_hard(op.worktree_path, op.start_head)
        self.db.clear_operation(repo_root)
        if op.worktree_path and op.worktree_branch:
            return (
                f"Aborted operation for {selectors.selector_for(repo_root, op.worktree_branch)}.\n"
                f"Inspect with: wt cd {selectors.selector_for(repo_root, op.worktree_branch)}"
            )
        return "Aborted operation."

    def _run_single_pp(self, tracked: TrackedBranch, logs: list[str], *, require_upstream_before: bool = False) -> bool:
        label = selectors.selector_for(tracked.repo_root, tracked.branch)
        upstream = git.upstream_branch(tracked.worktree_path)
        if require_upstream_before and not upstream:
            self._record(logs, f"Stopping at {label}: no upstream remote is configured.")
            return False
        self._record(logs, f"Running git pp --force in {label}")
        proc = git.pp_force(tracked.worktree_path)
        if proc.stdout.strip():
            for line in proc.stdout.strip().splitlines():
                self._record(logs, line)
        if proc.stderr.strip():
            for line in proc.stderr.strip().splitlines():
                self._record(logs, line)
        if proc.returncode != 0:
            raise git.GitError(f"git pp --force failed in {label}.\nInspect with: wt cd {label}")
        upstream_after = git.upstream_branch(tracked.worktree_path)
        if not upstream_after:
            self._record(logs, f"Stopping at {label}: no upstream remote is configured.")
            raise git.GitError(f"{label} has no upstream remote after git pp --force.")
        return True

    def _only_child(self, target: SelectorTarget) -> TrackedBranch:
        children = self.db.get_children(target.repo_root, target.worktree_path, target.branch)
        if not children:
            raise git.GitError(
                f"No tracked child worktree for {selectors.selector_for(target.repo_root, target.branch)}."
            )
        if len(children) > 1:
            raise git.GitError(
                "stacker next is not implemented for worktrees with multiple tracked children yet."
            )
        return children[0]

    def _pr_config(self, repo_root: str) -> RepoPRConfig:
        stored = self.db.get_repo_pr_config(repo_root)
        if stored:
            return stored
        return RepoPRConfig(
            repo_root=repo_root,
            mode="normal",
            trunk_branch=git.guess_trunk_branch(repo_root),
            main_repo=None,
        )

    def _current_repo_slug(self, repo_root: str) -> str:
        return gh.repo_info(cwd=repo_root).name_with_owner

    def _create_or_update_current_pr(
        self,
        tracked: TrackedBranch,
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,
        *,
        draft: bool,
        logs: list[str],
    ) -> gh.PullRequest:
        target_repo = self._target_repo_slug(config, current_repo)
        remote_branch = self._remote_branch_name(tracked)
        title, first_body = self._first_commit_text(tracked)
        body = self._compose_body_with_block(first_body, "")
        base = self._pr_base_for_current_branch(tracked, config, current_repo)
        existing = self._find_open_pr(tracked, config, current_repo)
        head = self._head_ref_for_branch(config, current_repo, remote_branch)
        if existing:
            self._record(logs, f"Updating PR #{existing.number} for {selectors.selector_for(tracked.repo_root, tracked.branch)}")
            gh.edit_pr(repo=target_repo, number=existing.number, title=title, base=base)
        else:
            self._record(logs, f"Creating PR for {selectors.selector_for(tracked.repo_root, tracked.branch)}")
            with self._body_file(body) as body_file:
                gh.create_pr(repo=target_repo, base=base, head=head, title=title, body_file=body_file, draft=draft)
        refreshed = self._find_open_pr(tracked, config, current_repo)
        if not refreshed:
            raise git.GitError(
                f"Could not find the open PR for {selectors.selector_for(tracked.repo_root, tracked.branch)} after create/update."
            )
        return refreshed

    def _refresh_component_pr_bodies(
        self,
        tracked: TrackedBranch,
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,
        current_pr: gh.PullRequest | None,
        logs: list[str],
    ) -> None:
        component = self._component_nodes(tracked)
        pr_map = self._open_pr_map(component, config, current_repo)
        if current_pr:
            pr_map[(tracked.worktree_path, tracked.branch)] = current_pr
        if not pr_map:
            return
        for node in component:
            pr = pr_map.get((node.worktree_path, node.branch))
            if not pr:
                continue
            block = self._render_stack_block(component, node, pr_map, config, current_repo)
            base_body = self._pr_base_body(pr)
            with self._body_file(self._compose_body_with_block(base_body, block)) as body_file:
                gh.edit_pr(repo=self._target_repo_slug(config, current_repo), number=pr.number, body_file=body_file)
            self._record(logs, f"Updated stack block for PR #{pr.number} ({selectors.selector_for(node.repo_root, node.branch)})")

    def _erase_component_pr_bodies(
        self,
        tracked: TrackedBranch,
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,
        logs: list[str],
    ) -> None:
        component = self._component_nodes(tracked)
        pr_map = self._open_pr_map(component, config, current_repo)
        if not pr_map:
            return
        for node in component:
            pr = pr_map.get((node.worktree_path, node.branch))
            if not pr:
                continue
            with self._body_file(self._pr_base_body(pr)) as body_file:
                gh.edit_pr(repo=self._target_repo_slug(config, current_repo), number=pr.number, body_file=body_file)
            self._record(logs, f"Erased stack block for PR #{pr.number} ({selectors.selector_for(node.repo_root, node.branch)})")

    def _component_nodes(self, tracked: TrackedBranch) -> list[TrackedBranch]:
        ancestors: list[TrackedBranch] = []
        current = tracked
        while True:
            parent = self.db.get_worktree(current.parent_repo_root, current.parent_worktree_path, current.parent_branch)
            if not parent:
                break
            ancestors.append(parent)
            current = parent
        ancestors.reverse()
        return [*ancestors, tracked, *self._toposorted_descendants(tracked.repo_root, tracked.worktree_path, tracked.branch)]

    def _open_pr_map(
        self,
        component: list[TrackedBranch],
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,
    ) -> dict[tuple[str, str], gh.PullRequest]:
        return {
            (node.worktree_path, node.branch): pr
            for node in component
            if (pr := self._find_open_pr(node, config, current_repo)) is not None
        }

    def _find_open_pr(
        self,
        tracked: TrackedBranch,
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,
    ) -> gh.PullRequest | None:
        target_repo = self._target_repo_slug(config, current_repo)
        remote_branch = git.upstream_branch_name(tracked.worktree_path)
        if not remote_branch:
            return None
        if config.mode == "forked":
            search = f"is:open head:{current_repo.owner}:{remote_branch}"
            prs = gh.list_open_prs(target_repo, search=search)
        else:
            prs = gh.list_open_prs(target_repo, head=remote_branch)
        return prs[0] if prs else None

    def _pr_base_for_current_branch(
        self,
        tracked: TrackedBranch,
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,
    ) -> str:
        if config.mode == "forked":
            return config.trunk_branch
        if tracked.parent_branch == config.trunk_branch:
            return config.trunk_branch
        parent_tracked = self.db.get_worktree(tracked.parent_repo_root, tracked.parent_worktree_path, tracked.parent_branch)
        if not parent_tracked:
            raise git.GitError(
                f"Direct parent {selectors.selector_for(tracked.parent_repo_root, tracked.parent_branch)} must already have an open PR."
            )
        parent_pr = self._find_open_pr(parent_tracked, config, current_repo)
        if not parent_pr:
            raise git.GitError(
                f"Direct parent {selectors.selector_for(parent_tracked.repo_root, parent_tracked.branch)} must already have an open PR."
            )
        parent_remote_branch = self._remote_branch_name(parent_tracked)
        return parent_remote_branch

    def _remote_branch_name(self, tracked: TrackedBranch) -> str:
        remote_branch = git.upstream_branch_name(tracked.worktree_path)
        if not remote_branch:
            raise git.GitError(
                f"{selectors.selector_for(tracked.repo_root, tracked.branch)} has no upstream remote branch. "
                f"Run stacker pp or push it first."
            )
        return remote_branch

    def _target_repo_slug(self, config: RepoPRConfig, current_repo: gh.RepoInfo) -> str:
        return config.main_repo or current_repo.name_with_owner

    def _head_ref_for_branch(self, config: RepoPRConfig, current_repo: gh.RepoInfo, remote_branch: str) -> str:
        if config.mode == "forked":
            return f"{current_repo.owner}:{remote_branch}"
        return remote_branch

    def _first_commit_text(self, tracked: TrackedBranch) -> tuple[str, str]:
        title, body = git.first_commit_title_and_body(
            tracked.worktree_path,
            f"{tracked.managed_base_commit}..HEAD",
        )
        if not title:
            title = tracked.branch
        return title, body

    def _compose_body_with_block(self, body: str, block: str) -> str:
        cleaned = self._strip_managed_block(body).strip()
        if block:
            if cleaned:
                return f"{cleaned}\n\n{block}"
            return block
        return cleaned

    def _strip_managed_block(self, body: str) -> str:
        return re.sub(
            r"\n?<!-- stacker:begin -->.*?<!-- stacker:end -->\n?",
            "\n",
            body,
            flags=re.S,
        ).strip()

    def _pr_base_body(self, pr: gh.PullRequest) -> str:
        return self._strip_managed_block(pr.body)

    def _render_stack_block(
        self,
        component: list[TrackedBranch],
        current_node: TrackedBranch,
        pr_map: dict[tuple[str, str], gh.PullRequest],
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,
    ) -> str:
        lines = ["<!-- stacker:begin -->", "## Stacker"]
        nav_links: list[str] = []
        prev_node = self.db.get_worktree(current_node.parent_repo_root, current_node.parent_worktree_path, current_node.parent_branch)
        if prev_node and (prev_pr := pr_map.get((prev_node.worktree_path, prev_node.branch))):
            nav_links.append(f"[<- ({prev_node.branch})]({prev_pr.url})")
        current_changes_url = self._compare_url(current_node, config, current_repo, pr_map)
        if current_changes_url:
            nav_links.append(f"**[{current_node.branch} (changes)]({current_changes_url})**")
        next_nodes = self.db.get_children(current_node.repo_root, current_node.worktree_path, current_node.branch)
        if len(next_nodes) == 1 and (next_pr := pr_map.get((next_nodes[0].worktree_path, next_nodes[0].branch))):
            nav_links.append(f"[({next_nodes[0].branch}) ->]({next_pr.url})")
        if nav_links:
            lines.append("&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;&nbsp;".join(nav_links))
        lines.append("")
        component_keys = {(node.worktree_path, node.branch) for node in component}
        roots = [
            node
            for node in component
            if (node.parent_worktree_path, node.parent_branch) not in component_keys
        ]
        for index, root in enumerate(sorted(roots, key=lambda item: item.branch)):
            if index:
                lines.append("")
            lines.extend(self._render_stack_lines(component, root, current_node, pr_map, config, current_repo, prefix=""))
        lines.append("<!-- stacker:end -->")
        return "\n".join(lines)

    def _render_stack_lines(
        self,
        component: list[TrackedBranch],
        node: TrackedBranch,
        current_node: TrackedBranch,
        pr_map: dict[tuple[str, str], gh.PullRequest],
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,
        *,
        prefix: str,
    ) -> list[str]:
        label = node.branch
        is_current = node.worktree_path == current_node.worktree_path and node.branch == current_node.branch
        formatted_label = f"<strong><code>{label}</code></strong>" if is_current else f"`{label}`"
        marker = " (current)" if is_current else ""
        parts = [f"{formatted_label}{marker}"]
        pr = pr_map.get((node.worktree_path, node.branch))
        if pr:
            parts.append(f"[PR #{pr.number}]({pr.url})")
        compare_url = self._compare_url(node, config, current_repo, pr_map)
        if compare_url:
            parts.append(f"[changes]({compare_url})")
        content = " ".join(parts)
        if is_current:
            content = f"**{content}**"
        lines = [f"{prefix}- {content}"]
        children = sorted(
            [child for child in component if child.parent_worktree_path == node.worktree_path and child.parent_branch == node.branch],
            key=lambda item: item.branch,
        )
        for child in children:
            lines.extend(self._render_stack_lines(component, child, current_node, pr_map, config, current_repo, prefix=prefix + "  "))
        return lines

    def _compare_url(
        self,
        node: TrackedBranch,
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,
        pr_map: dict[tuple[str, str], gh.PullRequest],
    ) -> str | None:
        target_repo = self._target_repo_slug(config, current_repo)
        try:
            head_sha = git.rev_parse(node.worktree_path, "HEAD")
        except git.GitError:
            return None
        base_sha = node.managed_base_commit
        return f"https://github.com/{target_repo}/compare/{quote(base_sha, safe=':/')}...{quote(head_sha, safe=':/')}"

    def _body_file(self, body: str):
        class _BodyFile:
            def __enter__(inner_self):
                inner_self.file = NamedTemporaryFile("w", delete=False, encoding="utf-8")
                inner_self.file.write(body)
                inner_self.file.flush()
                inner_self.file.close()
                inner_self.path = inner_self.file.name
                return inner_self.path

            def __exit__(inner_self, exc_type, exc, tb):
                Path(inner_self.path).unlink(missing_ok=True)
                return False

        return _BodyFile()

    def render_zsh(self) -> str:
        return (Path(__file__).with_name("stacker.zsh").read_text()).strip()

    def _prepare_local_operation(self, tracked: TrackedBranch, *, op_type: str, logs: list[str]) -> OperationState:
        parent_head, start_head, commit_list = self._sync_plan(tracked)
        op = self.db.get_operation(tracked.repo_root)
        if op is None:
            op = OperationState(repo_root=tracked.repo_root, op_type=op_type, status="running")
        op.status = "running"
        op.worktree_path = tracked.worktree_path
        op.worktree_branch = tracked.branch
        op.parent_path = tracked.parent_worktree_path
        op.parent_branch = tracked.parent_branch
        op.start_head = start_head
        op.target_parent_head = parent_head
        op.commit_list = commit_list
        op.next_commit_index = 0
        self.db.put_operation(op)
        self._record(
            logs,
            f"Resetting {selectors.selector_for(tracked.repo_root, tracked.branch)} "
            f"to {selectors.selector_for(tracked.parent_repo_root, tracked.parent_branch)} "
            f"({self._short(parent_head)})",
        )
        git.reset_hard(tracked.worktree_path, parent_head)
        return op

    def _run_until_pause_or_finish(self, repo_root: str, continuing: bool = False, logs: list[str] | None = None) -> str:
        if logs is None:
            logs = []
        op = self.db.get_operation(repo_root)
        assert op
        while True:
            if op.worktree_path:
                result = self._drive_local(repo_root, op, continuing=continuing, logs=logs)
                continuing = False
                if result is not None:
                    return self._finish(logs, result)
                op = self.db.get_operation(repo_root)
                assert op
                continue
            if op.op_type == "local_sync":
                self.db.clear_operation(repo_root)
                return self._finish(logs, "Sync complete.")
            if op.op_type == "downstream_sync":
                if op.current_index >= len(op.queue):
                    self.db.clear_operation(repo_root)
                    return self._finish(logs, "Push complete.")
                next_path = op.queue[op.current_index]
                branch = git.current_branch(next_path)
                tracked = self.db.get_worktree(repo_root, next_path, branch)
                if not tracked:
                    raise git.GitError(f"Tracked worktree disappeared from state: {next_path}")
                parent_head, _, _ = self._sync_plan(tracked)
                if parent_head == tracked.managed_base_commit:
                    self._record(
                        logs,
                        f"Skipping {selectors.selector_for(tracked.repo_root, tracked.branch)}; "
                        f"parent {selectors.selector_for(tracked.parent_repo_root, tracked.parent_branch)} "
                        f"is unchanged at {self._short(parent_head)}",
                    )
                    op.current_index += 1
                    self.db.put_operation(op)
                    continue
                self._prepare_local_operation(tracked, op_type="downstream_sync", logs=logs)
                op = self.db.get_operation(repo_root)
                assert op
                continue
            raise git.GitError(f"Unknown operation type {op.op_type}.")

    def _drive_local(self, repo_root: str, op: OperationState, *, continuing: bool, logs: list[str]) -> str | None:
        assert op.worktree_path
        path = op.worktree_path
        if continuing:
            op.next_commit_index = self._recompute_progress(path, op)
            if git.cherry_pick_in_progress(path):
                label = selectors.selector_for(repo_root, op.worktree_branch or git.current_branch(path))
                self._record(logs, f"Continuing cherry-pick in {label}")
                proc = git.cherry_pick_continue(path)
                if proc.returncode != 0:
                    message = proc.stderr.strip() or proc.stdout.strip() or "cherry-pick --continue failed"
                    if self._is_empty_cherry_pick_message(message):
                        commit = op.commit_list[op.next_commit_index]
                        skip = git.cherry_pick_skip(path)
                        if skip.returncode != 0:
                            op.status = "paused"
                            op.error_message = skip.stderr.strip() or skip.stdout.strip() or "cherry-pick --skip failed"
                            self.db.put_operation(op)
                            return self._failure_message(op)
                        self._record(logs, f"Skipping empty cherry-pick {self._short(commit)} on {label}")
                        op.next_commit_index += 1
                        op.error_message = None
                        op.status = "running"
                        self.db.put_operation(op)
                    else:
                        op.status = "paused"
                        op.error_message = message
                        self.db.put_operation(op)
                        return self._failure_message(op)
                else:
                    op.next_commit_index += 1
                    self.db.put_operation(op)
            elif self._should_skip_empty_commit(op):
                commit = op.commit_list[op.next_commit_index]
                self._record(
                    logs,
                    f"Skipping empty cherry-pick {self._short(commit)} on "
                    f"{selectors.selector_for(repo_root, op.worktree_branch or git.current_branch(path))}",
                )
                op.next_commit_index += 1
                op.error_message = None
                op.status = "running"
                self.db.put_operation(op)
        while op.next_commit_index < len(op.commit_list):
            commit = op.commit_list[op.next_commit_index]
            self._record(
                logs,
                f"Cherry-picking {self._short(commit)} onto "
                f"{selectors.selector_for(repo_root, op.worktree_branch or git.current_branch(path))}",
            )
            proc = git.cherry_pick(path, commit)
            if proc.returncode != 0:
                op.status = "paused"
                op.error_message = proc.stderr.strip() or proc.stdout.strip() or f"cherry-pick failed for {commit}"
                self.db.put_operation(op)
                return self._failure_message(op)
            op.next_commit_index += 1
            self.db.put_operation(op)
        tracked = self._require_tracked(SelectorTarget(repo_root=repo_root, worktree_path=path, branch=op.worktree_branch or git.current_branch(path)))
        updated = TrackedBranch(
            repo_root=tracked.repo_root,
            worktree_path=tracked.worktree_path,
            branch=tracked.branch,
            parent_repo_root=tracked.parent_repo_root,
            parent_worktree_path=tracked.parent_worktree_path,
            parent_branch=tracked.parent_branch,
            managed_base_commit=op.target_parent_head or tracked.managed_base_commit,
            last_synced_parent_commit=op.target_parent_head or tracked.last_synced_parent_commit,
            last_clean_head=git.rev_parse(path, "HEAD"),
        )
        self.db.upsert_worktree(updated)
        if op.op_type == "local_sync":
            self.db.clear_operation(repo_root)
            return "Sync complete."
        op.worktree_path = None
        op.worktree_branch = None
        op.parent_path = None
        op.parent_branch = None
        op.start_head = None
        op.target_parent_head = None
        op.commit_list = []
        op.next_commit_index = 0
        op.error_message = None
        op.current_index += 1
        op.status = "running"
        self.db.put_operation(op)
        return None

    def _recompute_progress(self, path: str, op: OperationState) -> int:
        if not op.target_parent_head:
            return op.next_commit_index
        current_head = git.rev_parse(path, "HEAD")
        if current_head == op.target_parent_head:
            return 0
        applied_count = git.rev_count(path, f"{op.target_parent_head}..{current_head}")
        if applied_count < 0:
            return 0
        return min(applied_count, len(op.commit_list))

    def _toposorted_descendants(self, repo_root: str, path: str, branch: str) -> list[TrackedBranch]:
        ordered: list[TrackedBranch] = []

        def walk(parent_path: str, parent_branch: str) -> None:
            children = self.db.get_children(repo_root, parent_path, parent_branch)
            for child in children:
                ordered.append(child)
                walk(child.worktree_path, child.branch)

        walk(path, branch)
        return ordered

    def _render_graph_node(
        self,
        lines: list[str],
        repo_root: str,
        path: str,
        branch: str,
        items: list[TrackedBranch],
        tracked_index: dict[tuple[str, str], TrackedBranch],
        live_worktrees: dict[tuple[str, str], git.WorktreeInfo],
        prefix: str,
        is_last: bool,
        implicit: bool,
        current,
    ) -> None:
        label = selectors.selector_for(repo_root, branch)
        branch_label = self._style(label, fg="green", bold=not implicit)
        suffixes: list[str] = []
        tracked = tracked_index.get((path, branch))
        if implicit:
            suffixes.append(self._style("[untracked root]", fg="yellow"))
        elif tracked:
            synced = self._is_synced(tracked)
            suffixes.append(self._style("[synced]" if synced else "[unsynced]", fg="cyan" if synced else "yellow"))
            if self._has_graph_blocking_changes(tracked, live_worktrees):
                suffixes.append(self._style("[dirty]", fg="red", bold=True))
        if current and current.repo_root == repo_root and current.worktree_path == path and current.branch == branch:
            branch_label = self._style(label, fg="magenta", bold=True)
            suffixes.append(self._style("[current]", fg="magenta", bold=True))
        connector = "└── " if is_last else "├── "
        line = f"{prefix}{connector}{branch_label}"
        if suffixes:
            line = f"{line} {' '.join(suffixes)}"
        lines.append(line)

        children = [
            item
            for item in items
            if item.parent_worktree_path == path and item.parent_branch == branch
        ]
        children.sort(key=lambda item: (item.branch, item.worktree_path))
        child_prefix = prefix + ("    " if is_last else "│   ")
        for index, child in enumerate(children):
            self._render_graph_node(
                lines,
                repo_root,
                child.worktree_path,
                child.branch,
                items,
                tracked_index,
                live_worktrees,
                child_prefix,
                index == len(children) - 1,
                False,
                current,
            )

    def _is_synced(self, tracked: TrackedBranch) -> bool:
        try:
            parent_head = git.rev_parse(tracked.parent_worktree_path, tracked.parent_branch)
        except git.GitError:
            return False
        return parent_head == tracked.managed_base_commit

    def _has_graph_blocking_changes(
        self,
        tracked: TrackedBranch,
        live_worktrees: dict[tuple[str, str], git.WorktreeInfo],
    ) -> bool:
        if (tracked.worktree_path, tracked.branch) not in live_worktrees:
            return False
        try:
            return git.has_tracked_changes(tracked.worktree_path)
        except git.GitError:
            return False

    def _ensure_syncable(self, path: str) -> None:
        if git.has_tracked_changes(path):
            raise git.GitError(f"Tracked changes present in {path}. Commit or discard them before syncing.")
        if git.cherry_pick_in_progress(path):
            raise git.GitError(f"Cherry-pick already in progress in {path}. Resolve it before starting another sync.")

    def _target_repo_for_create(self, branch_ref: str, current_repo_root: str | None) -> str:
        parsed = selectors.parse_selector(branch_ref)
        if parsed.repo_query is None:
            if current_repo_root is None:
                raise git.GitError("Not in a git repository; use [repo:]<branch>.")
            return current_repo_root
        return selectors.resolve_repo_path(parsed.repo_query, current_repo_root)

    def _current_context_or_none(self):
        try:
            return git.current_context()
        except Exception:
            return None

    def _require_tracked(self, target: SelectorTarget) -> TrackedBranch:
        tracked = self.db.get_worktree(target.repo_root, target.worktree_path, target.branch)
        if not tracked:
            raise git.GitError(
                f"{selectors.selector_for(target.repo_root, target.branch)} is not tracked. Use 'stacker track' or 'stacker create'."
            )
        return tracked

    def _failure_message(self, op: OperationState) -> str:
        assert op.worktree_branch
        inspect_selector = selectors.selector_for(op.repo_root, op.worktree_branch)
        parent_selector = selectors.selector_for(op.repo_root, op.parent_branch or "")
        parts = [
            f"Sync paused on {inspect_selector} while syncing onto {parent_selector}.\n"
            f"Cherry-pick in progress: {'yes' if git.cherry_pick_in_progress(op.worktree_path or '') else 'no'}"
        ]
        if op.error_message:
            parts.append(f"Git says: {op.error_message}")
        parts.append(f"Inspect with: wt cd {inspect_selector}")
        parts.append("Next action: stacker continue or stacker abort")
        return "\n".join(parts)

    def _sync_plan(self, tracked: TrackedBranch) -> tuple[str, str, list[str]]:
        parent_head = git.rev_parse(tracked.parent_worktree_path, tracked.parent_branch)
        start_head = git.rev_parse(tracked.worktree_path, "HEAD")
        commit_list = git.rev_list(tracked.worktree_path, f"{tracked.managed_base_commit}..{start_head}")
        return parent_head, start_head, commit_list

    def _short(self, commit: str) -> str:
        return commit[:12]

    def _should_skip_empty_commit(self, op: OperationState) -> bool:
        return (
            op.next_commit_index < len(op.commit_list)
            and bool(op.error_message)
            and self._is_empty_cherry_pick_message(op.error_message)
        )

    def _is_empty_cherry_pick_message(self, message: str) -> bool:
        return "previous cherry-pick is now empty" in message.lower()

    def _record(self, logs: list[str], message: str) -> None:
        logs.append(message)
        if self.progress:
            self.progress(message)

    def _finish(self, logs: list[str], final_message: str) -> str:
        if self.progress:
            return final_message
        return "\n".join([*logs, final_message]) if logs else final_message

    def _style(self, text: str, *, fg: str | None = None, bold: bool = False) -> str:
        if not self._use_color():
            return text
        codes: list[str] = []
        if bold:
            codes.append("1")
        colors = {
            "blue": "34",
            "green": "32",
            "yellow": "33",
            "magenta": "35",
            "cyan": "36",
            "red": "31",
        }
        if fg and fg in colors:
            codes.append(colors[fg])
        if not codes:
            return text
        return f"\033[{';'.join(codes)}m{text}\033[0m"

    def _use_color(self) -> bool:
        if os.environ.get("NO_COLOR"):
            return False
        if os.environ.get("CLICOLOR_FORCE"):
            return True
        return sys.stdout.isatty()
