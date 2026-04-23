from __future__ import annotations

import contextlib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote

from project_manager import output
from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OWNER_STACKER_OPS, PoolDB

from . import config_schema, gh, git, locate, ops_slot, selectors
from .db import StackerDB
from .models import (
    OperationState,
    ParentLocator,
    RepoPRConfig,
    SelectorTarget,
    TrackedBranch,
)
from .pr_backend import GhCliBackend, PRBackend


@dataclass
class _Acquired:
    """A worktree path to run git commands in, plus the ops slot if we acquired one.

    If `ops` is set, the caller claimed it via ops_slot.acquire. Clean completion
    must call release(). A paused/failed op must NOT release — the claim is the
    resumable state that `continue` / `abort` drive.
    """

    path: Path
    ops: slot_mod.Slot | None


@dataclass(frozen=True)
class _StackRender:
    """Shared context threaded through stack-block rendering recursion."""

    component: list[TrackedBranch]
    pr_map: dict[str, gh.PullRequest]
    config: RepoPRConfig
    current_repo: gh.RepoInfo


@dataclass(frozen=True)
class _GraphCtx:
    """Per-repo state for graph rendering; threaded through recursion."""

    repo_name: str
    items: list[TrackedBranch]
    live: dict[str, git.WorktreeInfo]


@dataclass(frozen=True)
class _GraphPos:
    """Position of a node in the rendered tree."""

    prefix: str
    is_last: bool
    implicit: bool


class StackerService:
    def __init__(
        self,
        db: StackerDB,
        paths: Paths,
        *,
        progress: Callable[[str], None] | None = None,
        pr_backend: PRBackend | None = None,
    ) -> None:
        self.db = db
        self.paths = paths
        self.progress = progress
        self.pr_backend: PRBackend = pr_backend or GhCliBackend()

    # -------------------------------------------------------------
    # High-level operations
    # -------------------------------------------------------------

    def initialize_worktree(
        self,
        repo_name: str,
        worktree_path: Path,
        branch: str,
        *,
        create_branch: bool,
        parent: ParentLocator | None,
    ) -> TrackedBranch | None:
        """Seed a caller-supplied slot with `branch` and optionally track it.

        - `create_branch=True`, `parent` required: create `branch` off the
          parent's tip, track with managed_base = parent_tip.
        - `create_branch=False`, `parent` given: check out existing `branch`,
          track with managed_base = merge-base(branch, parent_branch).
        - `create_branch=False`, `parent=None`: plain checkout, no tracking.
        """
        if parent is not None and parent.repo_name != repo_name:
            raise git.GitError("Parent and child must be in the same repo.")
        repo_path = self.paths.repo(repo_name)

        if create_branch:
            if parent is None:
                raise git.GitError("create_branch=True requires a parent.")
            parent_head = git.rev_parse(repo_path, parent.branch)
            git.git(worktree_path, "checkout", "-b", branch, parent_head)
            managed_base = parent_head
        else:
            git.git(worktree_path, "checkout", branch)
            if parent is None:
                return None
            managed_base = git.merge_base(repo_path, parent.branch, branch)

        assert parent is not None
        tracked = TrackedBranch(
            repo_name=repo_name,
            branch=branch,
            parent_repo_name=parent.repo_name,
            parent_branch=parent.branch,
            managed_base_commit=managed_base,
            last_synced_parent_commit=git.rev_parse(repo_path, parent.branch),
            last_clean_head=git.rev_parse(worktree_path, "HEAD"),
        )
        self.db.upsert_branch(tracked)
        return tracked

    def track(self, target: SelectorTarget, parent: ParentLocator) -> TrackedBranch:
        if target.repo_name != parent.repo_name:
            raise git.GitError("Parent and child must be in the same repo.")
        repo_path = self.paths.repo(target.repo_name)
        base_commit = git.merge_base(repo_path, parent.branch, target.branch)
        parent_head = git.rev_parse(repo_path, parent.branch)
        branch_slot = locate.locate_worktree(self.paths, target.repo_name, target.branch)
        if branch_slot is None:
            raise git.GitError(
                f"{selectors.selector_for(target.repo_name, target.branch)} is not checked out "
                "in any worktree; nothing to adopt."
            )
        tracked = TrackedBranch(
            repo_name=target.repo_name,
            branch=target.branch,
            parent_repo_name=parent.repo_name,
            parent_branch=parent.branch,
            managed_base_commit=base_commit,
            last_synced_parent_commit=parent_head,
            last_clean_head=git.rev_parse(branch_slot, "HEAD"),
        )
        self.db.upsert_branch(tracked)
        return tracked

    def untrack(self, target: SelectorTarget) -> str:
        if self.db.get_operation(target.repo_name):
            raise git.GitError(
                "Another stacker operation is active for this repo. "
                "Use 'stacker continue' or 'stacker abort'."
            )
        tracked = self.db.get_branch(target.repo_name, target.branch)
        if not tracked:
            raise git.GitError(
                f"{selectors.selector_for(target.repo_name, target.branch)} is not tracked."
            )
        for child in self.db.get_children(target.repo_name, target.branch):
            self.db.upsert_branch(
                TrackedBranch(
                    repo_name=child.repo_name,
                    branch=child.branch,
                    parent_repo_name=tracked.parent_repo_name,
                    parent_branch=tracked.parent_branch,
                    managed_base_commit=child.managed_base_commit,
                    last_synced_parent_commit=child.last_synced_parent_commit,
                    last_clean_head=child.last_clean_head,
                )
            )
        self.db.delete_branch(target.repo_name, target.branch)
        return f"Untracked {selectors.selector_for(target.repo_name, target.branch)}"

    def status_text(self, target: SelectorTarget) -> str:
        tracked = self.db.get_branch(target.repo_name, target.branch)
        op = self.db.get_operation(target.repo_name)
        slot_path = locate.locate_worktree(self.paths, target.repo_name, target.branch)
        lines = [selectors.selector_for(target.repo_name, target.branch)]
        lines.append(f"checked out in: {slot_path or '-'}")
        if tracked:
            parent_label = selectors.selector_for(
                tracked.parent_repo_name, tracked.parent_branch
            )
            lines.extend(
                [
                    f"parent: {parent_label}",
                    f"managed base: {tracked.managed_base_commit}",
                    f"last synced parent: {tracked.last_synced_parent_commit or '-'}",
                    f"last clean head: {tracked.last_clean_head or '-'}",
                ]
            )
        else:
            children = self.db.get_children(target.repo_name, target.branch)
            if children:
                lines.append(f"untracked root with {len(children)} tracked child(ren)")
            else:
                lines.append("not tracked")
        if op:
            lines.append(f"operation: {op.op_type} ({op.status})")
            if op.branch:
                lines.append(f"active branch: {op.branch}")
            if op.error_message:
                lines.append(f"last error: {op.error_message}")
        else:
            lines.append("operation: none")
        return "\n".join(lines)

    def log_text(self, target: SelectorTarget) -> str:
        tracked = self._require_tracked(target)
        path = self._require_checked_out(target.repo_name, target.branch)
        entries = git.log_subject_and_author(path, f"{tracked.managed_base_commit}..HEAD")
        if not entries:
            return (
                f"No commits since "
                f"{selectors.selector_for(tracked.parent_repo_name, tracked.parent_branch)} "
                f"at {self._short(tracked.managed_base_commit)}."
            )
        label = selectors.selector_for(tracked.repo_name, tracked.branch)
        lines = [f"{label} commits since parent:"]
        for subject, author in entries:
            lines.append(f"{subject} ({author})" if author else subject)
        return "\n".join(lines)

    def parent_text(self, target: SelectorTarget) -> str:
        tracked = self._require_tracked(target)
        return tracked.managed_base_commit

    def graph_text(self, repo_name: str | None = None) -> str:
        branches = self.db.list_branches(repo_name)
        if not branches:
            return "No tracked branches."
        by_repo: dict[str, list[TrackedBranch]] = {}
        for item in branches:
            by_repo.setdefault(item.repo_name, []).append(item)
        lines: list[str] = []
        for repo, items in sorted(by_repo.items()):
            repo_path = self.paths.repo(repo)
            try:
                live = {info.branch: info for info in git.worktree_list(repo_path) if info.branch}
            except git.GitError:
                live = {}
            lines.append(self._style(repo, fg="blue", bold=True))
            tracked_branches = {item.branch for item in items}
            roots: list[tuple[str, bool]] = []
            seen_roots: set[str] = set()
            for item in items:
                if (
                    item.parent_branch not in tracked_branches
                    and item.parent_branch not in seen_roots
                ):
                    roots.append((item.parent_branch, True))
                    seen_roots.add(item.parent_branch)
            ctx = _GraphCtx(repo_name=repo, items=items, live=live)
            for parent_branch, implicit in sorted(roots):
                self._render_graph_node(
                    lines,
                    ctx,
                    parent_branch,
                    _GraphPos(prefix="", is_last=True, implicit=implicit),
                )
        return "\n".join(lines)

    # -------------------------------------------------------------
    # PR configuration
    # -------------------------------------------------------------

    def get_config(self, repo_name: str, key: str) -> str | None:
        config_schema.require_known_key(key)
        return self.db.get_config(repo_name, key)

    def set_config(self, repo_name: str, key: str, value: str) -> list[str]:
        """Write one config key, enforcing cross-key invariants.

        Returns informational notices (e.g. auto-flip of pr.mode) for display.
        Only hits `gh repo view` when the key being set requires cross-check
        against the push remote.
        """
        config_schema.validate_value(key, value)
        notices: list[str] = []
        if key == config_schema.PR_MODE and value == "pr-pr":
            push_remote = self._push_remote_slug(repo_name)
            target = (
                self.db.get_config(repo_name, config_schema.PR_TARGET_REPO)
                or push_remote
            )
            if target != push_remote:
                raise git.GitError(
                    f"pr.mode=pr-pr requires pr.target-repo to equal the push "
                    f"remote ({push_remote}), but pr.target-repo is {target}. "
                    "Unset pr.target-repo first or switch to pr.mode=repo-pr."
                )
        self.db.set_config(repo_name, key, value)
        if key == config_schema.PR_TARGET_REPO:
            push_remote = self._push_remote_slug(repo_name)
            if value != push_remote:
                current_mode = config_schema.parse_mode(
                    self.db.get_config(repo_name, config_schema.PR_MODE)
                )
                if current_mode == "pr-pr":
                    self.db.set_config(repo_name, config_schema.PR_MODE, "repo-pr")
                    notices.append(
                        "Switched pr.mode to repo-pr because pr.target-repo "
                        f"({value}) differs from the push remote ({push_remote})."
                    )
        return notices

    def unset_config(self, repo_name: str, key: str) -> bool:
        config_schema.require_known_key(key)
        return self.db.unset_config(repo_name, key)

    def list_config(self, repo_name: str) -> list[tuple[str, str]]:
        return self.db.list_config(repo_name)

    # -------------------------------------------------------------
    # Sync / repair / push / pp / pr
    # -------------------------------------------------------------

    def sync(self, target: SelectorTarget) -> str:
        tracked = self._require_tracked(target)
        if self.db.get_operation(target.repo_name):
            raise git.GitError(
                "Another stacker operation is active for this repo. "
                "Use 'stacker continue' or 'stacker abort'."
            )
        # Write the operation row *before* claiming a slot so pm pool gc-ops
        # can always reconcile claim vs. active-op state.
        self.db.put_operation(
            OperationState(
                repo_name=target.repo_name,
                op_type="local_sync",
                status="running",
                branch=target.branch,
                parent_branch=tracked.parent_branch,
            )
        )
        try:
            acquired = self._acquire(target.repo_name, target.branch)
        except Exception:
            self.db.clear_operation(target.repo_name)
            raise
        parent_head, _, _ = self._sync_plan(tracked, acquired.path)
        if parent_head == tracked.managed_base_commit:
            self.db.clear_operation(target.repo_name)
            self._release_if_owned(acquired)
            child_label = selectors.selector_for(tracked.repo_name, tracked.branch)
            parent_label = selectors.selector_for(
                tracked.parent_repo_name, tracked.parent_branch
            )
            return (
                f"Nothing to sync for {child_label}.\n"
                f"Parent {parent_label} is unchanged at {self._short(parent_head)}."
            )
        try:
            self._ensure_syncable(acquired.path)
        except git.GitError:
            self.db.clear_operation(target.repo_name)
            self._release_if_owned(acquired)
            raise
        logs: list[str] = []
        self._prepare_local_operation(
            tracked, op_type="local_sync", slot_path=acquired.path, logs=logs
        )
        return self._run_until_pause_or_finish(
            target.repo_name,
            slot_path=acquired.path,
            acquired_ops=acquired.ops,
            logs=logs,
        )

    def repair(self, target: SelectorTarget, base_ref: str) -> str:
        tracked = self._require_tracked(target)
        if self.db.get_operation(target.repo_name):
            raise git.GitError(
                "Another stacker operation is active for this repo. "
                "Use 'stacker continue' or 'stacker abort'."
            )
        path = self._require_checked_out(target.repo_name, target.branch)
        self._ensure_syncable(path)
        actual_base = git.rev_parse(path, base_ref)
        current_head = git.rev_parse(path, "HEAD")
        label = selectors.selector_for(tracked.repo_name, tracked.branch)
        if (
            actual_base == tracked.managed_base_commit
            and actual_base == (tracked.last_synced_parent_commit or "")
            and current_head == (tracked.last_clean_head or "")
        ):
            return (
                f"No repair needed for {label}.\n"
                f"Stored base already matches {self._short(actual_base)}."
            )
        self.db.upsert_branch(
            TrackedBranch(
                repo_name=tracked.repo_name,
                branch=tracked.branch,
                parent_repo_name=tracked.parent_repo_name,
                parent_branch=tracked.parent_branch,
                managed_base_commit=actual_base,
                last_synced_parent_commit=actual_base,
                last_clean_head=current_head,
            )
        )
        return (
            f"Repaired {label}.\n"
            f"Stored base ref: {base_ref} -> {self._short(actual_base)}\n"
            f"Managed base: {self._short(tracked.managed_base_commit)} -> "
            f"{self._short(actual_base)}"
        )

    def push(self, target: SelectorTarget) -> str:
        if self.db.get_operation(target.repo_name):
            raise git.GitError(
                "Another stacker operation is active for this repo. "
                "Use 'stacker continue' or 'stacker abort'."
            )
        queue = self._toposorted_descendants(target.repo_name, target.branch)
        if not queue:
            return "No downstream tracked branches to sync."
        # Write op row before claiming any slot.
        self.db.put_operation(
            OperationState(
                repo_name=target.repo_name,
                op_type="downstream_sync",
                status="running",
                root_branch=target.branch,
                queue=[child.branch for child in queue],
                current_index=0,
            )
        )
        return self._run_until_pause_or_finish(target.repo_name, logs=[])

    def pp(
        self,
        target: SelectorTarget,
        *,
        force_stack_block: bool = False,
        erase_stack_block: bool = False,
    ) -> str:
        logs: list[str] = []
        queue: list[TrackedBranch] = []
        tracked = self.db.get_branch(target.repo_name, target.branch)
        if tracked:
            queue.append(tracked)
        queue.extend(self._toposorted_descendants(target.repo_name, target.branch))
        if not queue:
            return "No tracked branches to push."

        config = self._pr_config(target.repo_name)
        repo_path = self.paths.repo(target.repo_name)
        try:
            current_repo = self.pr_backend.repo_info(cwd=repo_path)
        except git.GitError:
            current_repo = None
        refresh_root: TrackedBranch | None = None
        for item in queue:
            if not self._run_single_pp(item, logs, require_upstream_before=True):
                return self._finish(logs, "PP complete.")
            if (
                current_repo
                and refresh_root is None
                and self._find_open_pr(item, config, current_repo)
            ):
                refresh_root = item
        should_refresh_block = (
            config.mode == "repo-pr" or force_stack_block or erase_stack_block
        )
        if should_refresh_block and current_repo and refresh_root is not None:
            if erase_stack_block:
                self._erase_component_pr_bodies(refresh_root, config, current_repo, logs)
            else:
                self._refresh_component_pr_bodies(refresh_root, config, current_repo, None, logs)
        return self._finish(logs, "PP complete.")

    def pr(self, target: SelectorTarget, *, draft: bool) -> str:
        tracked = self._require_tracked(target)
        config = self._pr_config(tracked.repo_name)
        repo_path = self.paths.repo(tracked.repo_name)
        logs: list[str] = []
        current_repo = self.pr_backend.repo_info(cwd=repo_path)
        self._run_single_pp(tracked, logs)
        current_pr = self._create_or_update_current_pr(
            tracked, config, current_repo, draft=draft, logs=logs
        )
        self._refresh_component_pr_bodies(tracked, config, current_repo, current_pr, logs)
        return self._finish(logs, f"PR ready: {current_pr.url}")

    def continue_operation(self, repo_name: str) -> str:
        op = self.db.get_operation(repo_name)
        if not op:
            raise git.GitError("No paused stacker operation for this repo.")
        slot_path = self._slot_path_for_active_op(op)
        acquired_ops = self._existing_ops_slot(op, slot_path)
        return self._run_until_pause_or_finish(
            repo_name,
            slot_path=slot_path,
            acquired_ops=acquired_ops,
            continuing=True,
            logs=[],
        )

    def abort_operation(self, repo_name: str) -> str:
        op = self.db.get_operation(repo_name)
        if not op:
            raise git.GitError("No active stacker operation for this repo.")
        slot_path = self._slot_path_for_active_op(op, required=False)
        if slot_path and git.cherry_pick_in_progress(slot_path):
            git.cherry_pick_abort(slot_path)
        if slot_path and op.start_head:
            git.reset_hard(slot_path, op.start_head)
        ops = self._existing_ops_slot(op, slot_path) if slot_path else None
        self.db.clear_operation(repo_name)
        if ops is not None:
            ops_slot.release(PoolDB(self.paths.pool_db()), ops)
        if op.branch:
            return f"Aborted operation for {selectors.selector_for(repo_name, op.branch)}."
        return "Aborted operation."

    def guard_no_rebase(self) -> None:
        """Hook entry point: error out if cwd is on a stacker-tracked branch.

        Requires cwd to be inside a worktree in a known pm repo.
        """
        context = git.current_context()
        repo_name = self._repo_name_for_path(context.worktree_path)
        if repo_name is None:
            return
        tracked = self.db.get_branch(repo_name, context.branch)
        if not tracked:
            return
        label = selectors.selector_for(repo_name, context.branch)
        if self.db.get_operation(repo_name):
            raise git.GitError(
                f"{label} is managed by stacker and has an active stacker operation. "
                "Do not rebase it; use 'stacker continue' or 'stacker abort'."
            )
        raise git.GitError(
            f"{label} is managed by stacker. "
            "Do not rebase it; use stacker sync/push/repair instead."
        )

    # -------------------------------------------------------------
    # Slot acquisition
    # -------------------------------------------------------------

    def _acquire(self, repo_name: str, branch: str) -> _Acquired:
        existing = locate.locate_worktree(self.paths, repo_name, branch)
        if existing is not None:
            return _Acquired(path=existing, ops=None)
        claimed = ops_slot.acquire(self.paths, PoolDB(self.paths.pool_db()), repo_name, branch)
        return _Acquired(path=claimed.path, ops=claimed)

    def _release_if_owned(self, acquired: _Acquired) -> None:
        if acquired.ops is not None:
            ops_slot.release(PoolDB(self.paths.pool_db()), acquired.ops)

    def _slot_path_for_active_op(
        self, op: OperationState, *, required: bool = True
    ) -> Path | None:
        if op.branch is None:
            return None
        path = locate.locate_worktree(self.paths, op.repo_name, op.branch)
        if path is None:
            if required:
                raise git.GitError(
                    f"Operation references {selectors.selector_for(op.repo_name, op.branch)} "
                    "but the branch is not checked out anywhere."
                )
            return None
        return path

    def _existing_ops_slot(
        self, op: OperationState, slot_path: Path | None
    ) -> slot_mod.Slot | None:
        """Return the stacker-ops Slot handle backing this op's current branch, or None.

        A slot is an ops slot if the pool db records it as stacker-owned. A
        project-owned slot (or otherwise-claimed slot) returns None — we don't own it.
        """
        if slot_path is None:
            return None
        expected_pool = self.paths.pool(op.repo_name).resolve()
        if slot_path.parent.resolve() != expected_pool:
            return None
        pooldb = PoolDB(self.paths.pool_db())
        if pooldb.get_owner(op.repo_name, slot_path.name) != OWNER_STACKER_OPS:
            return None
        return slot_mod.Slot(repo=op.repo_name, uuid=slot_path.name, path=slot_path)

    def _require_checked_out(self, repo_name: str, branch: str) -> Path:
        path = locate.locate_worktree(self.paths, repo_name, branch)
        if path is None:
            raise git.GitError(
                f"{selectors.selector_for(repo_name, branch)} is not checked out in any "
                "worktree. Check it out in a pm pool slot first."
            )
        return path

    def _repo_name_for_path(self, worktree_path: Path) -> str | None:
        resolved = worktree_path.resolve()
        try:
            relative = resolved.relative_to(self.paths.worktrees.resolve())
        except ValueError:
            return None
        parts = relative.parts
        if not parts:
            return None
        return parts[0]

    # -------------------------------------------------------------
    # PR helpers (inherited logic; all paths resolved via locate)
    # -------------------------------------------------------------

    def _run_single_pp(
        self,
        tracked: TrackedBranch,
        logs: list[str],
        *,
        require_upstream_before: bool = False,
    ) -> bool:
        label = selectors.selector_for(tracked.repo_name, tracked.branch)
        path = self._require_checked_out(tracked.repo_name, tracked.branch)
        upstream = git.upstream_branch(path)
        if require_upstream_before and not upstream:
            self._record(logs, f"Stopping at {label}: no upstream remote is configured.")
            return False
        self._record(logs, f"Running git pp --force in {label}")
        proc = git.pp_force(path)
        if proc.stdout.strip():
            for line in proc.stdout.strip().splitlines():
                self._record(logs, line)
        if proc.stderr.strip():
            for line in proc.stderr.strip().splitlines():
                self._record(logs, line)
        if proc.returncode != 0:
            raise git.GitError(f"git pp --force failed in {label}.")
        upstream_after = git.upstream_branch(path)
        if not upstream_after:
            self._record(logs, f"Stopping at {label}: no upstream remote is configured.")
            raise git.GitError(f"{label} has no upstream remote after git pp --force.")
        return True

    def _pr_config(self, repo_name: str) -> RepoPRConfig:
        mode = config_schema.parse_mode(self.db.get_config(repo_name, config_schema.PR_MODE))
        trunk = self.db.get_config(repo_name, config_schema.PR_TRUNK)
        target = self.db.get_config(repo_name, config_schema.PR_TARGET_REPO)
        return RepoPRConfig(
            repo_name=repo_name,
            mode=mode,
            trunk_branch=trunk or git.guess_trunk_branch(self.paths.repo(repo_name)),
            target_repo=target or self._push_remote_slug(repo_name),
        )

    def _push_remote_slug(self, repo_name: str) -> str:
        return self.pr_backend.repo_info(cwd=self.paths.repo(repo_name)).name_with_owner

    def _create_or_update_current_pr(
        self,
        tracked: TrackedBranch,
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,
        *,
        draft: bool,
        logs: list[str],
    ) -> gh.PullRequest:
        target_repo = self._target_repo_slug(config)
        remote_branch = self._remote_branch_name(tracked)
        title, first_body = self._first_commit_text(tracked)
        body = self._compose_body_with_block(first_body, "")
        base = self._pr_base_for_current_branch(tracked, config, current_repo)
        existing = self._find_open_pr(tracked, config, current_repo)
        head = self._head_ref_for_branch(config, current_repo, remote_branch)
        label = selectors.selector_for(tracked.repo_name, tracked.branch)
        if existing:
            self._record(logs, f"Updating PR #{existing.number} for {label}")
            self.pr_backend.edit_pr(
                gh.EditPRRequest(
                    repo=target_repo, number=existing.number, title=title, base=base
                )
            )
        else:
            self._record(logs, f"Creating PR for {label}")
            with self._body_file(body) as body_file:
                self.pr_backend.create_pr(
                    gh.CreatePRRequest(
                        repo=target_repo,
                        base=base,
                        head=head,
                        title=title,
                        body_file=body_file,
                        draft=draft,
                    )
                )
        refreshed = self._find_open_pr(tracked, config, current_repo)
        if not refreshed:
            raise git.GitError(f"Could not find the open PR for {label} after create/update.")
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
            pr_map[tracked.branch] = current_pr
        if not pr_map:
            return
        ctx = _StackRender(
            component=component, pr_map=pr_map, config=config, current_repo=current_repo,
        )
        for node in component:
            pr = pr_map.get(node.branch)
            if not pr:
                continue
            block = self._render_stack_block(ctx, node)
            base_body = self._pr_base_body(pr)
            with self._body_file(self._compose_body_with_block(base_body, block)) as body_file:
                self.pr_backend.edit_pr(
                    gh.EditPRRequest(
                        repo=self._target_repo_slug(config),
                        number=pr.number,
                        body_file=body_file,
                    )
                )
            self._record(
                logs,
                f"Updated stack block for PR #{pr.number} "
                f"({selectors.selector_for(node.repo_name, node.branch)})",
            )

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
            pr = pr_map.get(node.branch)
            if not pr:
                continue
            with self._body_file(self._pr_base_body(pr)) as body_file:
                self.pr_backend.edit_pr(
                    gh.EditPRRequest(
                        repo=self._target_repo_slug(config),
                        number=pr.number,
                        body_file=body_file,
                    )
                )
            self._record(
                logs,
                f"Erased stack block for PR #{pr.number} "
                f"({selectors.selector_for(node.repo_name, node.branch)})",
            )

    def _component_nodes(self, tracked: TrackedBranch) -> list[TrackedBranch]:
        ancestors: list[TrackedBranch] = []
        current = tracked
        while True:
            parent = self.db.get_branch(current.parent_repo_name, current.parent_branch)
            if not parent:
                break
            ancestors.append(parent)
            current = parent
        ancestors.reverse()
        return [
            *ancestors,
            tracked,
            *self._toposorted_descendants(tracked.repo_name, tracked.branch),
        ]

    def _open_pr_map(
        self,
        component: list[TrackedBranch],
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,
    ) -> dict[str, gh.PullRequest]:
        return {
            node.branch: pr
            for node in component
            if (pr := self._find_open_pr(node, config, current_repo)) is not None
        }

    def _find_open_pr(
        self,
        tracked: TrackedBranch,
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,
    ) -> gh.PullRequest | None:
        target_repo = self._target_repo_slug(config)
        path = locate.locate_worktree(self.paths, tracked.repo_name, tracked.branch)
        if path is None:
            return None
        remote_branch = git.upstream_branch_name(path)
        if not remote_branch:
            return None
        if target_repo != current_repo.name_with_owner:
            search = f"is:open head:{current_repo.owner}:{remote_branch}"
            prs = self.pr_backend.list_open_prs(target_repo, search=search)
        else:
            prs = self.pr_backend.list_open_prs(target_repo, head=remote_branch)
        return prs[0] if prs else None

    def _pr_base_for_current_branch(
        self,
        tracked: TrackedBranch,
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,
    ) -> str:
        if config.mode == "repo-pr":
            return config.trunk_branch
        if tracked.parent_branch == config.trunk_branch:
            return config.trunk_branch
        parent_tracked = self.db.get_branch(tracked.parent_repo_name, tracked.parent_branch)
        parent_label = selectors.selector_for(tracked.parent_repo_name, tracked.parent_branch)
        if not parent_tracked:
            raise git.GitError(f"Direct parent {parent_label} must already have an open PR.")
        parent_pr = self._find_open_pr(parent_tracked, config, current_repo)
        if not parent_pr:
            raise git.GitError(f"Direct parent {parent_label} must already have an open PR.")
        return self._remote_branch_name(parent_tracked)

    def _remote_branch_name(self, tracked: TrackedBranch) -> str:
        path = self._require_checked_out(tracked.repo_name, tracked.branch)
        remote_branch = git.upstream_branch_name(path)
        if not remote_branch:
            raise git.GitError(
                f"{selectors.selector_for(tracked.repo_name, tracked.branch)} has no upstream "
                "remote branch. Run stacker pp or push it first."
            )
        return remote_branch

    def _target_repo_slug(self, config: RepoPRConfig) -> str:
        return config.target_repo

    def _head_ref_for_branch(
        self,
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,
        remote_branch: str,
    ) -> str:
        if config.target_repo != current_repo.name_with_owner:
            return f"{current_repo.owner}:{remote_branch}"
        return remote_branch

    def _first_commit_text(self, tracked: TrackedBranch) -> tuple[str, str]:
        path = self._require_checked_out(tracked.repo_name, tracked.branch)
        title, body = git.first_commit_title_and_body(
            path, f"{tracked.managed_base_commit}..HEAD"
        )
        if not title:
            title = tracked.branch
        return title, body

    def _compose_body_with_block(self, body: str, block: str) -> str:
        cleaned = self._strip_managed_block(body).strip()
        if block:
            return f"{cleaned}\n\n{block}" if cleaned else block
        return cleaned

    def _strip_managed_block(self, body: str) -> str:
        return re.sub(
            r"\n?<!-- stacker:begin -->.*?<!-- stacker:end -->\n?",
            "\n",
            body,
            flags=re.DOTALL,
        ).strip()

    def _pr_base_body(self, pr: gh.PullRequest) -> str:
        return self._strip_managed_block(pr.body)

    def _render_stack_block(
        self,
        ctx: _StackRender,
        current_node: TrackedBranch,
    ) -> str:
        lines = ["<!-- stacker:begin -->", "## Stacker"]
        nav_links: list[str] = []
        show_compare = ctx.config.mode == "repo-pr"
        prev_node = self.db.get_branch(current_node.parent_repo_name, current_node.parent_branch)
        if prev_node and (prev_pr := ctx.pr_map.get(prev_node.branch)):
            nav_links.append(f"[<- ({prev_node.branch})]({prev_pr.url})")
        if show_compare:
            current_changes_url = self._compare_url(current_node, ctx)
            if current_changes_url:
                nav_links.append(
                    f"**[{current_node.branch} (changes)]({current_changes_url})**"
                )
        else:
            nav_links.append(f"**{current_node.branch}**")
        next_nodes = self.db.get_children(current_node.repo_name, current_node.branch)
        if len(next_nodes) == 1 and (next_pr := ctx.pr_map.get(next_nodes[0].branch)):
            nav_links.append(f"[({next_nodes[0].branch}) ->]({next_pr.url})")
        if nav_links:
            lines.append("&nbsp;&nbsp;&nbsp;|&nbsp;&nbsp;&nbsp;".join(nav_links))
        lines.append("")
        component_keys = {node.branch for node in ctx.component}
        roots = [
            node for node in ctx.component if node.parent_branch not in component_keys
        ]
        for index, root in enumerate(sorted(roots, key=lambda item: item.branch)):
            if index:
                lines.append("")
            lines.extend(self._render_stack_lines(ctx, root, current_node, prefix=""))
        lines.append("<!-- stacker:end -->")
        return "\n".join(lines)

    def _render_stack_lines(
        self,
        ctx: _StackRender,
        node: TrackedBranch,
        current_node: TrackedBranch,
        *,
        prefix: str,
    ) -> list[str]:
        label = node.branch
        is_current = node.branch == current_node.branch
        formatted_label = (
            f"<strong><code>{label}</code></strong>" if is_current else f"`{label}`"
        )
        marker = " (current)" if is_current else ""
        parts = [f"{formatted_label}{marker}"]
        pr = ctx.pr_map.get(node.branch)
        if pr:
            parts.append(f"[PR #{pr.number}]({pr.url})")
        if ctx.config.mode == "repo-pr":
            compare_url = self._compare_url(node, ctx)
            if compare_url:
                parts.append(f"[changes]({compare_url})")
        content = " ".join(parts)
        if is_current:
            content = f"**{content}**"
        lines = [f"{prefix}- {content}"]
        children = sorted(
            [child for child in ctx.component if child.parent_branch == node.branch],
            key=lambda item: item.branch,
        )
        for child in children:
            lines.extend(self._render_stack_lines(ctx, child, current_node, prefix=prefix + "  "))
        return lines

    def _compare_url(self, node: TrackedBranch, ctx: _StackRender) -> str | None:
        target_repo = self._target_repo_slug(ctx.config)
        path = locate.locate_worktree(self.paths, node.repo_name, node.branch)
        if path is None:
            return None
        try:
            head_sha = git.rev_parse(path, "HEAD")
        except git.GitError:
            return None
        base_sha = node.managed_base_commit
        return (
            f"https://github.com/{target_repo}/compare/"
            f"{quote(base_sha, safe=':/')}...{quote(head_sha, safe=':/')}"
        )

    @contextlib.contextmanager
    def _body_file(self, body: str) -> Iterator[Path]:
        """Yield a path to a temp file holding `body`; unlinked on exit."""
        file = NamedTemporaryFile("w", delete=False, encoding="utf-8")  # noqa: SIM115
        try:
            file.write(body)
            file.flush()
            file.close()
            yield Path(file.name)
        finally:
            Path(file.name).unlink(missing_ok=True)

    # -------------------------------------------------------------
    # Operation driver
    # -------------------------------------------------------------

    def _prepare_local_operation(
        self,
        tracked: TrackedBranch,
        *,
        op_type: str,
        slot_path: Path,
        logs: list[str],
    ) -> OperationState:
        parent_head, start_head, commit_list = self._sync_plan(tracked, slot_path)
        op = self.db.get_operation(tracked.repo_name)
        if op is None:
            op = OperationState(
                repo_name=tracked.repo_name, op_type=op_type, status="running"
            )
        op.status = "running"
        op.branch = tracked.branch
        op.parent_branch = tracked.parent_branch
        op.start_head = start_head
        op.target_parent_head = parent_head
        op.commit_list = commit_list
        op.next_commit_index = 0
        self.db.put_operation(op)
        self._record(
            logs,
            f"Resetting {selectors.selector_for(tracked.repo_name, tracked.branch)} to "
            f"{selectors.selector_for(tracked.parent_repo_name, tracked.parent_branch)} "
            f"({self._short(parent_head)})",
        )
        git.reset_hard(slot_path, parent_head)
        return op

    def _run_until_pause_or_finish(
        self,
        repo_name: str,
        *,
        slot_path: Path | None = None,
        acquired_ops: slot_mod.Slot | None = None,
        continuing: bool = False,
        logs: list[str] | None = None,
    ) -> str:
        if logs is None:
            logs = []
        op = self.db.get_operation(repo_name)
        assert op
        while True:
            if op.branch:
                if slot_path is None:
                    slot_path = self._require_checked_out(repo_name, op.branch)
                    acquired_ops = self._existing_ops_slot(op, slot_path)
                result = self._drive_local(
                    repo_name, op, slot_path=slot_path, continuing=continuing, logs=logs
                )
                continuing = False
                if result is not None:
                    if acquired_ops is not None:
                        ops_slot.release(PoolDB(self.paths.pool_db()), acquired_ops)
                    return self._finish(logs, result)
                op = self.db.get_operation(repo_name)
                assert op
                slot_path, acquired_ops = None, None
                continue
            if op.op_type == "local_sync":
                self.db.clear_operation(repo_name)
                return self._finish(logs, "Sync complete.")
            if op.op_type == "downstream_sync":
                if op.current_index >= len(op.queue):
                    self.db.clear_operation(repo_name)
                    return self._finish(logs, "Push complete.")
                slot_path, acquired_ops = self._advance_downstream(repo_name, op, logs)
                op = self.db.get_operation(repo_name)
                assert op
                continue
            raise git.GitError(f"Unknown operation type {op.op_type}.")

    def _advance_downstream(
        self, repo_name: str, op: OperationState, logs: list[str]
    ) -> tuple[Path | None, slot_mod.Slot | None]:
        """Move the downstream_sync queue forward by one entry. Returns
        (slot_path, acquired_ops) for the newly in-flight local op, or
        (None, None) if the entry was skipped (parent unchanged)."""
        next_branch = op.queue[op.current_index]
        tracked = self.db.get_branch(repo_name, next_branch)
        if not tracked:
            raise git.GitError(
                f"Tracked branch disappeared from state: "
                f"{selectors.selector_for(repo_name, next_branch)}"
            )
        acquired = self._acquire(repo_name, next_branch)
        parent_head, _, _ = self._sync_plan(tracked, acquired.path)
        if parent_head == tracked.managed_base_commit:
            child_label = selectors.selector_for(tracked.repo_name, tracked.branch)
            parent_label = selectors.selector_for(
                tracked.parent_repo_name, tracked.parent_branch
            )
            self._record(
                logs,
                f"Skipping {child_label}; "
                f"parent {parent_label} is unchanged at {self._short(parent_head)}",
            )
            op.current_index += 1
            self.db.put_operation(op)
            self._release_if_owned(acquired)
            return None, None
        self._prepare_local_operation(
            tracked, op_type="downstream_sync", slot_path=acquired.path, logs=logs
        )
        return acquired.path, acquired.ops

    def _drive_local(
        self,
        repo_name: str,
        op: OperationState,
        *,
        slot_path: Path,
        continuing: bool,
        logs: list[str],
    ) -> str | None:
        assert op.branch
        if continuing:
            result = self._resume_cherry_pick(repo_name, op, slot_path, logs)
            if result is not None:
                return result
        pause = self._cherry_pick_remaining(repo_name, op, slot_path, logs)
        if pause is not None:
            return pause
        return self._finalize_local_op(repo_name, op, slot_path)

    def _resume_cherry_pick(
        self,
        repo_name: str,
        op: OperationState,
        slot_path: Path,
        logs: list[str],
    ) -> str | None:
        assert op.branch
        op.next_commit_index = self._recompute_progress(slot_path, op)
        if git.cherry_pick_in_progress(slot_path):
            label = selectors.selector_for(repo_name, op.branch)
            self._record(logs, f"Continuing cherry-pick in {label}")
            proc = git.cherry_pick_continue(slot_path)
            if proc.returncode != 0:
                message = (
                    proc.stderr.strip()
                    or proc.stdout.strip()
                    or "cherry-pick --continue failed"
                )
                if self._is_empty_cherry_pick_message(message):
                    return self._resume_skip_empty(op, slot_path, logs, label)
                op.status = "paused"
                op.error_message = message
                self.db.put_operation(op)
                return self._failure_message(op, slot_path)
            op.next_commit_index += 1
            self.db.put_operation(op)
        elif self._should_skip_empty_commit(op):
            commit = op.commit_list[op.next_commit_index]
            self._record(
                logs,
                f"Skipping empty cherry-pick {self._short(commit)} on "
                f"{selectors.selector_for(repo_name, op.branch)}",
            )
            op.next_commit_index += 1
            op.error_message = None
            op.status = "running"
            self.db.put_operation(op)
        return None

    def _resume_skip_empty(
        self, op: OperationState, slot_path: Path, logs: list[str], label: str
    ) -> str | None:
        commit = op.commit_list[op.next_commit_index]
        skip = git.cherry_pick_skip(slot_path)
        if skip.returncode != 0:
            op.status = "paused"
            op.error_message = (
                skip.stderr.strip() or skip.stdout.strip() or "cherry-pick --skip failed"
            )
            self.db.put_operation(op)
            return self._failure_message(op, slot_path)
        self._record(logs, f"Skipping empty cherry-pick {self._short(commit)} on {label}")
        op.next_commit_index += 1
        op.error_message = None
        op.status = "running"
        self.db.put_operation(op)
        return None

    def _cherry_pick_remaining(
        self,
        repo_name: str,
        op: OperationState,
        slot_path: Path,
        logs: list[str],
    ) -> str | None:
        assert op.branch
        while op.next_commit_index < len(op.commit_list):
            commit = op.commit_list[op.next_commit_index]
            self._record(
                logs,
                f"Cherry-picking {self._short(commit)} onto "
                f"{selectors.selector_for(repo_name, op.branch)}",
            )
            proc = git.cherry_pick(slot_path, commit)
            if proc.returncode != 0:
                op.status = "paused"
                op.error_message = (
                    proc.stderr.strip()
                    or proc.stdout.strip()
                    or f"cherry-pick failed for {commit}"
                )
                self.db.put_operation(op)
                return self._failure_message(op, slot_path)
            op.next_commit_index += 1
            self.db.put_operation(op)
        return None

    def _finalize_local_op(
        self, repo_name: str, op: OperationState, slot_path: Path
    ) -> str | None:
        assert op.branch
        tracked = self._require_tracked(
            SelectorTarget(repo_name=repo_name, branch=op.branch)
        )
        self.db.upsert_branch(
            TrackedBranch(
                repo_name=tracked.repo_name,
                branch=tracked.branch,
                parent_repo_name=tracked.parent_repo_name,
                parent_branch=tracked.parent_branch,
                managed_base_commit=op.target_parent_head or tracked.managed_base_commit,
                last_synced_parent_commit=(
                    op.target_parent_head or tracked.last_synced_parent_commit
                ),
                last_clean_head=git.rev_parse(slot_path, "HEAD"),
            )
        )
        if op.op_type == "local_sync":
            self.db.clear_operation(repo_name)
            return "Sync complete."
        op.branch = None
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

    def _recompute_progress(self, path: Path, op: OperationState) -> int:
        if not op.target_parent_head:
            return op.next_commit_index
        current_head = git.rev_parse(path, "HEAD")
        if current_head == op.target_parent_head:
            return 0
        applied_count = git.rev_count(path, f"{op.target_parent_head}..{current_head}")
        if applied_count < 0:
            return 0
        return min(applied_count, len(op.commit_list))

    # -------------------------------------------------------------
    # Graph
    # -------------------------------------------------------------

    def _render_graph_node(
        self,
        lines: list[str],
        ctx: _GraphCtx,
        branch: str,
        pos: _GraphPos,
    ) -> None:
        label = selectors.selector_for(ctx.repo_name, branch)
        branch_label = self._style(label, fg="green", bold=not pos.implicit)
        suffixes: list[str] = []
        tracked = next((item for item in ctx.items if item.branch == branch), None)
        if pos.implicit:
            suffixes.append(self._style("[untracked root]", fg="yellow"))
        elif tracked:
            synced = self._is_synced(tracked)
            suffixes.append(
                self._style(
                    "[synced]" if synced else "[unsynced]",
                    fg="cyan" if synced else "yellow",
                )
            )
            if branch in ctx.live and self._has_graph_blocking_changes(ctx.live[branch].path):
                suffixes.append(self._style("[dirty]", fg="red", bold=True))
        connector = "└── " if pos.is_last else "├── "
        line = f"{pos.prefix}{connector}{branch_label}"
        if suffixes:
            line = f"{line} {' '.join(suffixes)}"
        lines.append(line)

        children = sorted(
            (item for item in ctx.items if item.parent_branch == branch),
            key=lambda item: item.branch,
        )
        child_prefix = pos.prefix + ("    " if pos.is_last else "│   ")
        for index, child in enumerate(children):
            self._render_graph_node(
                lines,
                ctx,
                child.branch,
                _GraphPos(
                    prefix=child_prefix,
                    is_last=index == len(children) - 1,
                    implicit=False,
                ),
            )

    def _is_synced(self, tracked: TrackedBranch) -> bool:
        try:
            parent_head = git.rev_parse(
                self.paths.repo(tracked.parent_repo_name), tracked.parent_branch
            )
        except git.GitError:
            return False
        return parent_head == tracked.managed_base_commit

    def _has_graph_blocking_changes(self, path: Path) -> bool:
        with contextlib.suppress(git.GitError):
            return git.has_tracked_changes(path)
        return False

    # -------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------

    def _ensure_syncable(self, path: Path) -> None:
        if git.has_tracked_changes(path):
            raise git.GitError(
                f"Tracked changes present in {path}. Commit or discard them before syncing."
            )
        if git.cherry_pick_in_progress(path):
            raise git.GitError(
                f"Cherry-pick already in progress in {path}. Resolve it before starting "
                "another sync."
            )

    def _toposorted_descendants(
        self, repo_name: str, branch: str
    ) -> list[TrackedBranch]:
        ordered: list[TrackedBranch] = []

        def walk(parent_branch: str) -> None:
            for child in self.db.get_children(repo_name, parent_branch):
                ordered.append(child)
                walk(child.branch)

        walk(branch)
        return ordered

    def _require_tracked(self, target: SelectorTarget) -> TrackedBranch:
        tracked = self.db.get_branch(target.repo_name, target.branch)
        if not tracked:
            raise git.GitError(
                f"{selectors.selector_for(target.repo_name, target.branch)} is not tracked. "
                "Use 'stacker track' or 'stacker create'."
            )
        return tracked

    def _failure_message(self, op: OperationState, slot_path: Path) -> str:
        assert op.branch
        inspect_selector = selectors.selector_for(op.repo_name, op.branch)
        parent_selector = selectors.selector_for(op.repo_name, op.parent_branch or "")
        cherry = git.cherry_pick_in_progress(slot_path)
        parts = [
            f"Sync paused on {inspect_selector} while syncing onto {parent_selector}.\n"
            f"Cherry-pick in progress: {'yes' if cherry else 'no'}"
        ]
        if op.error_message:
            parts.append(f"Git says: {op.error_message}")
        parts.append(f"Worktree at: {slot_path}")
        parts.append("Next action: stacker continue or stacker abort")
        return "\n".join(parts)

    def _sync_plan(
        self, tracked: TrackedBranch, slot_path: Path
    ) -> tuple[str, str, list[str]]:
        repo_path = self.paths.repo(tracked.parent_repo_name)
        parent_head = git.rev_parse(repo_path, tracked.parent_branch)
        start_head = git.rev_parse(slot_path, "HEAD")
        commit_list = git.rev_list(
            slot_path, f"{tracked.managed_base_commit}..{start_head}"
        )
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
        return output.style(text, fg=fg, bold=bold)
