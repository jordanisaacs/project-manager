from __future__ import annotations

import contextlib
import json
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
    DEFAULT_PUSH_OPTIONS,
    DEFAULT_SCOPE,
    Details,
    OperationState,
    ParentLocator,
    PushOptions,
    RepoPRConfig,
    Scope,
    ScopeSpec,
    SelectorTarget,
    TrackedBranch,
    WorktreeInit,
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

    def init_new_branch(self, spec: WorktreeInit) -> TrackedBranch:
        """Seed `spec.worktree_path` with a freshly-created, tracked branch.

        Creates the branch off `spec.parent`'s tip (or `spec.copy_from`'s
        tip when set — commits between the two become the branch's own
        cherry-pick set on next sync), then tracks it with
        managed_base = parent_tip.
        """
        if spec.parent is None:
            raise git.GitError("init_new_branch requires a parent.")
        if spec.parent.repo_name != spec.repo_name:
            raise git.GitError("Parent and child must be in the same repo.")
        repo_path = self.paths.repo(spec.repo_name)
        parent_head = git.rev_parse(repo_path, spec.parent.branch)
        start_point = (
            git.rev_parse(repo_path, spec.copy_from) if spec.copy_from else parent_head
        )
        git.git(spec.worktree_path, "checkout", "-b", spec.branch, start_point)
        return self._persist_init(spec, parent_head, parent_head)

    def init_adopt_branch(self, spec: WorktreeInit) -> TrackedBranch | None:
        """Check out `spec.branch` in `spec.worktree_path`, optionally tracking.

        With `spec.parent` set, tracks the branch using
        managed_base = merge-base(branch, parent_branch). Without a
        parent this is a plain checkout with no DB row written.
        """
        git.git(spec.worktree_path, "checkout", spec.branch)
        if spec.parent is None:
            return None
        if spec.parent.repo_name != spec.repo_name:
            raise git.GitError("Parent and child must be in the same repo.")
        repo_path = self.paths.repo(spec.repo_name)
        managed_base = git.merge_base(repo_path, spec.parent.branch, spec.branch)
        return self._persist_init(spec, managed_base, None)

    def _persist_init(
        self,
        spec: WorktreeInit,
        managed_base: str,
        last_synced_override: str | None,
    ) -> TrackedBranch:
        assert spec.parent is not None
        repo_path = self.paths.repo(spec.repo_name)
        last_synced = (
            last_synced_override
            if last_synced_override is not None
            else git.rev_parse(repo_path, spec.parent.branch)
        )
        tracked = TrackedBranch(
            repo_name=spec.repo_name,
            branch=spec.branch,
            parent_repo_name=spec.parent.repo_name,
            parent_branch=spec.parent.branch,
            managed_base_commit=managed_base,
            last_synced_parent_commit=last_synced,
            last_clean_head=git.rev_parse(spec.worktree_path, "HEAD"),
        )
        self.db.upsert_branch(tracked)
        return tracked

    def create_tracked_branch(
        self,
        repo_name: str,
        branch: str,
        parent: ParentLocator,
        *,
        copy_from: str | None = None,
    ) -> TrackedBranch:
        """Create a branch ref in the repo and track it without claiming a slot.

        Backs `create --no-checkout`: the branch exists in git's ref
        store and the stacker DB, but no pool slot is allocated for it.
        """
        if parent.repo_name != repo_name:
            raise git.GitError("Parent and child must be in the same repo.")
        repo_path = self.paths.repo(repo_name)
        if git.branch_exists(repo_path, branch):
            raise git.GitError(f"Branch {branch} already exists.")
        parent_head = git.rev_parse(repo_path, parent.branch)
        start_point = (
            git.rev_parse(repo_path, copy_from) if copy_from else parent_head
        )
        git.git(repo_path, "branch", branch, start_point)
        tracked = TrackedBranch(
            repo_name=repo_name,
            branch=branch,
            parent_repo_name=parent.repo_name,
            parent_branch=parent.branch,
            managed_base_commit=parent_head,
            last_synced_parent_commit=parent_head,
            last_clean_head=start_point,
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

    def remove(
        self,
        target: SelectorTarget,
        *,
        keep_branch: bool = False,
        parent_cascade: bool = False,
        force: bool = False,  # noqa: ARG002 — reserved for future interactive-confirm gate
    ) -> str:
        """Stop tracking a branch; optionally delete it and/or cascade upward.

        Default: untrack, reparent children onto the removed branch's
        parent, and delete the underlying git branch. `keep_branch=True`
        preserves the git branch (matches the old `untrack` semantics).
        `parent_cascade=True` also removes every tracked ancestor chain
        above the target (children of those ancestors get reparented up).
        Refuses when an op is paused on this repo.
        """
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
        removed: list[str] = []
        cursor: TrackedBranch | None = tracked
        while cursor is not None:
            self._remove_one(cursor, keep_branch=keep_branch)
            removed.append(selectors.selector_for(cursor.repo_name, cursor.branch))
            if not parent_cascade:
                break
            cursor = self.db.get_branch(cursor.parent_repo_name, cursor.parent_branch)
        if len(removed) == 1:
            return f"Removed {removed[0]}."
        return "Removed:\n  " + "\n  ".join(removed)

    def _remove_one(self, tracked: TrackedBranch, *, keep_branch: bool) -> None:
        """Untrack `tracked`, reparent children, optionally delete the git branch."""
        for child in self.db.get_children(tracked.repo_name, tracked.branch):
            self.db.upsert_branch(
                TrackedBranch(
                    repo_name=child.repo_name,
                    branch=child.branch,
                    parent_repo_name=tracked.parent_repo_name,
                    parent_branch=tracked.parent_branch,
                    managed_base_commit=child.managed_base_commit,
                    last_synced_parent_commit=child.last_synced_parent_commit,
                    last_clean_head=child.last_clean_head,
                    pr_url=child.pr_url,
                )
            )
        self.db.delete_branch(tracked.repo_name, tracked.branch)
        if keep_branch:
            return
        slot_path = locate.locate_worktree(self.paths, tracked.repo_name, tracked.branch)
        if slot_path is not None:
            # Detach so `git branch -D` won't refuse because it's checked out.
            git.git(slot_path, "checkout", "--detach", "HEAD")
        repo_path = self.paths.repo(tracked.repo_name)
        git.git(repo_path, "branch", "-D", tracked.branch, check=False)

    def reparent(self, target: SelectorTarget, new_parent: ParentLocator) -> str:
        """Move `target` onto a new parent; cascade cherry-pick through descendants.

        DB-only rewrite of `parent_branch`; `managed_base_commit` stays
        at the old parent's tip so the next sync sees every commit from
        `old_base..HEAD` as "to cherry-pick onto the new parent". Sync
        then updates managed_base to the new parent's tip on finalize.
        """
        if self.db.get_operation(target.repo_name):
            raise git.GitError(
                "Another stacker operation is active for this repo. "
                "Use 'stacker continue' or 'stacker abort'."
            )
        if new_parent.repo_name != target.repo_name:
            raise git.GitError("Cross-repo reparent is not supported.")
        tracked = self._require_tracked(target)
        if new_parent.branch == tracked.branch:
            raise git.GitError(f"{tracked.branch} cannot be its own parent.")
        if self._is_descendant(tracked, new_parent.branch):
            raise git.GitError(
                f"{new_parent.branch} is a descendant of {tracked.branch}; "
                "reparenting would create a cycle."
            )
        repo_path = self.paths.repo(tracked.repo_name)
        if not git.branch_exists(repo_path, new_parent.branch):
            raise git.GitError(
                f"Branch '{new_parent.branch}' not found in {tracked.repo_name}."
            )
        self.db.upsert_branch(
            TrackedBranch(
                repo_name=tracked.repo_name,
                branch=tracked.branch,
                parent_repo_name=new_parent.repo_name,
                parent_branch=new_parent.branch,
                managed_base_commit=tracked.managed_base_commit,
                last_synced_parent_commit=tracked.last_synced_parent_commit,
                last_clean_head=tracked.last_clean_head,
                pr_url=tracked.pr_url,
            )
        )
        return self.sync(
            target, ScopeSpec(scope="current", skip_ancestors=True)
        )

    def _is_descendant(self, tracked: TrackedBranch, candidate: str) -> bool:
        for descendant in self._toposorted_descendants(tracked.repo_name, tracked.branch):
            if descendant.branch == candidate:
                return True
        return False

    def split(
        self,
        target: SelectorTarget,
        new_name: str,
        split_commit: str,
        *,
        stay: bool = False,
    ) -> str:
        """Split `[split_commit..HEAD]` onto a new child branch `new_name`.

        Front-to-back: earlier commits stay on `target`, `split_commit`
        and everything after it move to `new_name`. Children of `target`
        reparent onto `new_name` (they were based on target's old tip,
        which is now `new_name`'s tip). Refuses dirty worktrees and any
        attempt to split at the first commit of the branch.

        After the split, `new_name` is provisioned into a free pool slot
        so the user has a workspace for it. Pass `stay=True` to skip
        the slot claim (the branch ref and DB row still land) — useful
        when the pool is saturated or the user only wanted to rearrange
        history. A saturated pool under the default is surfaced in the
        result message rather than raising, since the split itself has
        already succeeded.
        """
        tracked, current_path, plan = self._plan_split(target, new_name, split_commit)
        split_sha, head_sha, new_base_sha, moved_count = plan
        git.git(current_path, "branch", new_name, head_sha)
        git.reset_hard(current_path, new_base_sha)
        children = self.db.get_children(tracked.repo_name, tracked.branch)
        self.db.upsert_branch(
            TrackedBranch(
                repo_name=tracked.repo_name,
                branch=new_name,
                parent_repo_name=tracked.repo_name,
                parent_branch=tracked.branch,
                managed_base_commit=new_base_sha,
                last_synced_parent_commit=new_base_sha,
                last_clean_head=head_sha,
            )
        )
        for child in children:
            if child.branch == new_name:
                continue
            self.db.upsert_branch(
                TrackedBranch(
                    repo_name=child.repo_name,
                    branch=child.branch,
                    parent_repo_name=tracked.repo_name,
                    parent_branch=new_name,
                    managed_base_commit=child.managed_base_commit,
                    last_synced_parent_commit=child.last_synced_parent_commit,
                    last_clean_head=child.last_clean_head,
                    pr_url=child.pr_url,
                )
            )
        header = (
            f"Split {tracked.branch} at {self._short(split_sha)}: "
            f"{moved_count} commit(s) moved to {new_name}."
        )
        if stay:
            return header
        try:
            self._acquire(tracked.repo_name, new_name)
        except slot_mod.PoolExhaustedError as exc:
            return f"{header}\nNote: could not claim a slot for {new_name}: {exc}"
        return f"{header}\n{new_name} is now checked out in a fresh slot."

    def _plan_split(
        self, target: SelectorTarget, new_name: str, split_commit: str,
    ) -> tuple[TrackedBranch, Path, tuple[str, str, str, int]]:
        """Validate preconditions and compute the four sha/count values split needs.

        Returns (tracked, worktree_path, (split_sha, head_sha, new_base_sha,
        moved_count)). Raises on any precondition violation before any ref
        or DB state is mutated.
        """
        if self.db.get_operation(target.repo_name):
            raise git.GitError(
                "Another stacker operation is active for this repo. "
                "Use 'stacker continue' or 'stacker abort'."
            )
        tracked = self._require_tracked(target)
        current_path = self._require_checked_out(tracked.repo_name, tracked.branch)
        if git.has_tracked_changes(current_path):
            raise git.GitError(
                f"Tracked changes present in {current_path}. Commit or discard before splitting."
            )
        if git.cherry_pick_in_progress(current_path):
            raise git.GitError(
                f"Cherry-pick in progress in {current_path}. Resolve before splitting."
            )
        if git.branch_exists(self.paths.repo(tracked.repo_name), new_name):
            raise git.GitError(f"Branch {new_name} already exists.")
        split_sha = git.rev_parse(current_path, split_commit)
        head_sha = git.rev_parse(current_path, "HEAD")
        commit_list = git.rev_list(
            current_path, f"{tracked.managed_base_commit}..{head_sha}",
        )
        if split_sha not in commit_list:
            raise git.GitError(
                f"commit {split_commit} is not on {tracked.branch} "
                f"between {self._short(tracked.managed_base_commit)} and HEAD."
            )
        split_index = commit_list.index(split_sha)
        if split_index == 0:
            raise git.GitError("Cannot split at the first commit of this branch.")
        new_base_sha = commit_list[split_index - 1]
        moved_count = len(commit_list) - split_index
        return tracked, current_path, (split_sha, head_sha, new_base_sha, moved_count)

    def rename(self, target: SelectorTarget, new_name: str) -> str:
        """Rename the current branch; rewrite DB rows for it and its children.

        Does not touch the remote branch or PR head-ref: the next `push`
        will create the new remote branch; the old one lingers until
        cleaned up externally. `git branch -m` also drops the upstream
        config, so the next push must re-establish it.
        """
        if self.db.get_operation(target.repo_name):
            raise git.GitError(
                "Another stacker operation is active for this repo. "
                "Use 'stacker continue' or 'stacker abort'."
            )
        tracked = self._require_tracked(target)
        if target.branch == new_name:
            return f"Branch already named {new_name}."
        repo_path = self.paths.repo(tracked.repo_name)
        if git.branch_exists(repo_path, new_name):
            raise git.GitError(f"Branch {new_name} already exists.")
        current_path = self._require_checked_out(tracked.repo_name, tracked.branch)
        children = self.db.get_children(tracked.repo_name, target.branch)
        git.git(current_path, "branch", "-m", target.branch, new_name)
        self.db.delete_branch(tracked.repo_name, target.branch)
        self.db.upsert_branch(
            TrackedBranch(
                repo_name=tracked.repo_name,
                branch=new_name,
                parent_repo_name=tracked.parent_repo_name,
                parent_branch=tracked.parent_branch,
                managed_base_commit=tracked.managed_base_commit,
                last_synced_parent_commit=tracked.last_synced_parent_commit,
                last_clean_head=tracked.last_clean_head,
                pr_url=tracked.pr_url,
            )
        )
        for child in children:
            self.db.upsert_branch(
                TrackedBranch(
                    repo_name=child.repo_name,
                    branch=child.branch,
                    parent_repo_name=tracked.repo_name,
                    parent_branch=new_name,
                    managed_base_commit=child.managed_base_commit,
                    last_synced_parent_commit=child.last_synced_parent_commit,
                    last_clean_head=child.last_clean_head,
                    pr_url=child.pr_url,
                )
            )
        return f"Renamed {target.branch} -> {new_name}."

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

    def ls_text(
        self,
        repo_name: str | None = None,
        *,
        target_branch: str | None = None,
        scope: Scope = "all",
        details: Details = "status-counts",
        json_output: bool = False,
    ) -> str:
        """Render the stack tree with configurable detail level.

        Replaces the old `graph_text` (`pm stacker graph`) and the
        per-branch `status` command. `scope="current"` narrows the render
        to the lineage of `target_branch`; `scope="all"` shows every
        tracked branch grouped by repo. `details` controls per-branch
        annotations (none/status/status-counts/all). `json_output` emits
        a machine-readable tree instead of ANSI-styled text.
        """
        branches = self._ls_branch_set(repo_name, target_branch, scope)
        if not branches:
            return "No tracked branches."
        if json_output:
            return self._ls_json(branches, details)
        return self._ls_tree(branches, details)

    def _ls_branch_set(
        self,
        repo_name: str | None,
        target_branch: str | None,
        scope: Scope,
    ) -> list[TrackedBranch]:
        if scope == "current":
            if repo_name is None or target_branch is None:
                raise git.GitError(
                    "ls --scope current requires a repo and target branch."
                )
            target = self._require_tracked(
                SelectorTarget(repo_name=repo_name, branch=target_branch)
            )
            return self._lineage(target)
        return self.db.list_branches(repo_name)

    def _ls_tree(self, branches: list[TrackedBranch], details: Details) -> str:
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
                    details=details,
                )
        return "\n".join(lines)

    def _ls_json(self, branches: list[TrackedBranch], details: Details) -> str:
        """Machine-readable tree: one node per tracked branch.

        `details` controls which fields each node includes; `"none"`
        emits just names + parent links, fuller levels add op/sync
        state. Output is a flat list so consumers can rebuild the tree
        via parent_branch.
        """
        include_state = details in ("status", "status-counts", "all")
        include_counts = details in ("status-counts", "all")
        include_all = details == "all"
        entries: list[dict[str, object]] = []
        for item in branches:
            entry: dict[str, object] = {
                "repo_name": item.repo_name,
                "branch": item.branch,
                "parent_repo_name": item.parent_repo_name,
                "parent_branch": item.parent_branch,
            }
            if include_state:
                entry["synced"] = self._is_synced(item)
            if include_counts:
                entry["managed_base"] = item.managed_base_commit
            if include_all:
                entry["last_synced_parent_commit"] = item.last_synced_parent_commit
                entry["last_clean_head"] = item.last_clean_head
                entry["pr_url"] = item.pr_url
            entries.append(entry)
        return json.dumps(entries, indent=2)

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

    def sync(self, target: SelectorTarget, spec: ScopeSpec = DEFAULT_SCOPE) -> str:
        """Cherry-pick a set of tracked branches onto their parents.

        Scope resolution (see `_resolve_scope`) picks the branches. With a
        single-branch resolution we take the local_sync fast path so
        callers see the "Nothing to sync" short-circuit; with a multi-
        branch resolution we drive a downstream_sync queue that syncs
        each entry in parent-before-child order.
        """
        if self.db.get_operation(target.repo_name):
            raise git.GitError(
                "Another stacker operation is active for this repo. "
                "Use 'stacker continue' or 'stacker abort'."
            )
        resolved = self._resolve_scope(target.repo_name, target.branch, spec)
        if not resolved:
            return "No tracked branches to sync."
        if len(resolved) == 1:
            return self._sync_one(resolved[0])
        self.db.put_operation(
            OperationState(
                repo_name=target.repo_name,
                op_type="downstream_sync",
                status="running",
                root_branch=resolved[0].branch,
                queue=[b.branch for b in resolved],
                current_index=0,
            )
        )
        return self._run_until_pause_or_finish(target.repo_name, logs=[])

    def _sync_one(self, tracked: TrackedBranch) -> str:
        """Single-branch sync path: local_sync op with up-to-date short-circuit."""
        self.db.put_operation(
            OperationState(
                repo_name=tracked.repo_name,
                op_type="local_sync",
                status="running",
                branch=tracked.branch,
                parent_branch=tracked.parent_branch,
            )
        )
        try:
            acquired = self._acquire(tracked.repo_name, tracked.branch)
        except Exception:
            self.db.clear_operation(tracked.repo_name)
            raise
        parent_head, _, _ = self._sync_plan(tracked, acquired.path)
        if parent_head == tracked.managed_base_commit:
            self.db.clear_operation(tracked.repo_name)
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
            self.db.clear_operation(tracked.repo_name)
            self._release_if_owned(acquired)
            raise
        logs: list[str] = []
        self._prepare_local_operation(
            tracked, op_type="local_sync", slot_path=acquired.path, logs=logs
        )
        return self._run_until_pause_or_finish(
            tracked.repo_name,
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
                pr_url=tracked.pr_url,
            )
        )
        return (
            f"Repaired {label}.\n"
            f"Stored base ref: {base_ref} -> {self._short(actual_base)}\n"
            f"Managed base: {self._short(tracked.managed_base_commit)} -> "
            f"{self._short(actual_base)}"
        )

    def push(
        self, target: SelectorTarget, options: PushOptions = DEFAULT_PUSH_OPTIONS,
    ) -> str:
        """Force-push and create/update PRs for a set of tracked branches.

        Replaces the old `pr()` command. Scope resolution matches `sync`;
        within the resolved list each branch is force-pushed and has its
        PR created or updated. `options.publish` overrides per-branch
        draft decisions: default draft behavior is only-leaf-non-draft;
        with `--publish` every PR is published, with `--draft` every PR
        is draft. `options.create_pr=False` skips PR creation and only
        force-pushes.
        """
        if options.publish and options.draft:
            raise git.GitError("--publish and --draft are mutually exclusive.")
        if self.db.get_operation(target.repo_name):
            raise git.GitError(
                "Another stacker operation is active for this repo. "
                "Use 'stacker continue' or 'stacker abort'."
            )
        resolved = self._resolve_scope(target.repo_name, target.branch, options.scope)
        if not resolved:
            return "No tracked branches to push."
        config = self._pr_config(target.repo_name)
        repo_path = self.paths.repo(target.repo_name)
        logs: list[str] = []
        current_repo = (
            self.pr_backend.repo_info(cwd=repo_path) if options.create_pr else None
        )
        leaf = resolved[-1]
        latest_pr: gh.PullRequest | None = None
        for item in resolved:
            acquired = self._acquire(item.repo_name, item.branch)
            try:
                self._run_single_pp(item, logs)
                if options.create_pr and current_repo is not None:
                    is_leaf = item.branch == leaf.branch
                    item_draft = self._pr_draft_decision(
                        draft=options.draft, publish=options.publish, is_leaf=is_leaf,
                    )
                    latest_pr = self._create_or_update_current_pr(
                        item, config, current_repo, draft=item_draft, logs=logs
                    )
            finally:
                if item.branch != target.branch:
                    self._release_if_owned(acquired)
        if not options.create_pr or current_repo is None:
            return self._finish(logs, "Push complete.")
        assert latest_pr is not None
        refreshed = self._require_tracked(target)
        self._refresh_component_pr_bodies(
            refreshed, config, current_repo, latest_pr, logs,
        )
        return self._finish(logs, f"PR ready: {latest_pr.url}")

    def _pr_draft_decision(self, *, draft: bool, publish: bool, is_leaf: bool) -> bool:
        """Pick draft-ness per-branch when push walks a scope.

        `--draft` → every PR draft. `--publish` → every PR published.
        Default → non-leaf entries draft, leaf published (mirrors
        gitstack's "first PR off master published, deeper draft" model,
        but indexed from the leaf for pm's current-lineage default).
        """
        if draft:
            return True
        if publish:
            return False
        return not is_leaf

    def _ancestor_chain(self, tracked: TrackedBranch) -> list[TrackedBranch]:
        """Return the tracked ancestor chain, root-first, including `tracked`.

        Walks `parent_repo_name`/`parent_branch` upward until we hit an
        untracked parent (typically the trunk branch). Used by `pr` so that
        running `pm stacker pr <leaf>` pushes and opens PRs for every
        tracked ancestor before the leaf, in root→leaf order.
        """
        ancestors: list[TrackedBranch] = []
        cursor: TrackedBranch | None = tracked
        while cursor is not None:
            ancestors.append(cursor)
            cursor = self.db.get_branch(cursor.parent_repo_name, cursor.parent_branch)
        ancestors.reverse()
        return ancestors

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

        Fails open: if cwd isn't in a pm worktree, isn't in any git repo, or is
        on a detached HEAD, we can't identify a tracked branch and must let
        git proceed — the guard only blocks when it's certain.
        """
        try:
            context = git.current_context()
        except git.GitError:
            return
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
        claimed = ops_slot.acquire(
            self.paths,
            PoolDB(self.paths.pool_db()),
            repo_name,
            branch,
            wait=ops_slot.WaitOptions(progress=self.progress),
        )
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
        # pp_force streams its output directly to the parent's stdout/stderr,
        # so the user sees push progress live instead of waiting silently.
        proc = git.pp_force(path)
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

    def _head_repo_for_branch(self, tracked: TrackedBranch) -> str | None:
        """Return the `owner/name` slug of the repo the branch is pushed to.

        Detects cross-repo same-owner fork setups (like
        databricks-eng/universe-dev → databricks-eng/universe), which
        `gh pr create` cannot handle (cli/cli#10093). Returns None when the
        upstream isn't set, the remote URL can't be parsed, or we're in a
        test fixture without a real git remote — callers fall back to the
        default `gh pr create` path.
        """
        path = locate.locate_worktree(self.paths, tracked.repo_name, tracked.branch)
        if path is None:
            return None
        remote = git.upstream_remote_name(path)
        if not remote:
            return None
        url = git.remote_url(path, remote)
        if not url:
            return None
        return git.parse_github_slug(url)

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
        head_repo = self._head_repo_for_branch(tracked)
        title, first_body = self._first_commit_text(tracked)
        body = self._compose_body_with_block(first_body, "")
        base = self._pr_base_for_current_branch(tracked, config, current_repo)
        existing = self._find_open_pr(tracked, config, current_repo)
        head = self._head_ref_for_branch(config, current_repo, remote_branch)
        label = selectors.selector_for(tracked.repo_name, tracked.branch)
        with self._body_file(body) as body_file:
            if existing:
                self._record(logs, f"Updating PR #{existing.number} for {label}")
                self.pr_backend.edit_pr(
                    gh.EditPRRequest(
                        repo=target_repo,
                        number=existing.number,
                        title=title,
                        base=base,
                        body_file=body_file,
                    )
                )
                pr_url = existing.url
            else:
                self._record(logs, f"Creating PR for {label}")
                pr_url = self.pr_backend.create_pr(
                    gh.CreatePRRequest(
                        repo=target_repo,
                        base=base,
                        head=head,
                        title=title,
                        body_file=body_file,
                        draft=draft,
                        head_repo=head_repo,
                    )
                )
        # Cache the URL on the branch so future runs skip search entirely
        # (universe gitstack's StackItem.pr model).
        self._record_pr_url(tracked, pr_url)
        refreshed = self.pr_backend.view_pr(pr_url)
        if not refreshed:
            raise git.GitError(f"Could not fetch PR {pr_url} after create/update.")
        return refreshed

    def _refresh_component_pr_bodies(
        self,
        tracked: TrackedBranch,
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,
        current_pr: gh.PullRequest | None,
        logs: list[str],
    ) -> None:
        component = self._lineage(tracked)
        pr_map = self._pr_map_for_component(component, config, current_repo)
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
        component = self._lineage(tracked)
        pr_map = self._pr_map_for_component(component, config, current_repo)
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

    def _lineage(self, tracked: TrackedBranch) -> list[TrackedBranch]:
        """Return tracked ancestors root-first + `tracked` + descendants toposorted.

        Used as the `-c`/`--current` scope for sync/push/ls and as the
        component for PR-body rendering. Ancestors stop at the first
        untracked parent (matching `_ancestor_chain`'s termination).
        """
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

    def _toposorted_all(self, repo_name: str) -> list[TrackedBranch]:
        """All tracked branches in `repo_name`, ordered parent-before-child.

        Branches whose parent is not tracked (typical roots off trunk) go
        first, in stable `branch` order. Remaining branches follow a DFS
        from each root so a cascading op can rely on parents being synced
        before their children.
        """
        all_branches = self.db.list_branches(repo_name)
        by_parent: dict[str, list[TrackedBranch]] = {}
        tracked_names: set[str] = set()
        for b in all_branches:
            by_parent.setdefault(b.parent_branch, []).append(b)
            tracked_names.add(b.branch)
        ordered: list[TrackedBranch] = []
        visited: set[str] = set()

        def walk(parent_branch: str) -> None:
            for child in sorted(by_parent.get(parent_branch, []), key=lambda b: b.branch):
                if child.branch in visited:
                    continue
                visited.add(child.branch)
                ordered.append(child)
                walk(child.branch)

        roots = sorted(
            (b for b in all_branches if b.parent_branch not in tracked_names),
            key=lambda b: (b.parent_branch, b.branch),
        )
        for root in roots:
            if root.branch in visited:
                continue
            visited.add(root.branch)
            ordered.append(root)
            walk(root.branch)
        return ordered

    def _resolve_scope(
        self, repo_name: str, target_branch: str, spec: ScopeSpec,
    ) -> list[TrackedBranch]:
        """Resolve scope flags into an ordered branch list.

        Order is parent-before-child, safe to sync/push sequentially:
        `_lineage` for scope="current", `_toposorted_all` for scope="all".
        `only` short-circuits to just the target. `skip_ancestors` /
        `skip_descendants` trim the walk. `from_branch` drops entries
        before that branch in the resolved list.
        """
        if spec.only:
            tracked = self._require_tracked(
                SelectorTarget(repo_name=repo_name, branch=target_branch)
            )
            return [tracked]
        if spec.scope == "all":
            resolved = self._toposorted_all(repo_name)
            if spec.from_branch:
                resolved = _drop_until(resolved, spec.from_branch)
            return resolved
        target = self._require_tracked(
            SelectorTarget(repo_name=repo_name, branch=target_branch)
        )
        lineage = self._lineage(target)
        target_index = next(i for i, b in enumerate(lineage) if b.branch == target.branch)
        start = target_index if spec.skip_ancestors else 0
        end = target_index + 1 if spec.skip_descendants else len(lineage)
        resolved = lineage[start:end]
        if spec.from_branch:
            resolved = _drop_until(resolved, spec.from_branch)
        return resolved

    def _pr_map_for_component(
        self,
        component: list[TrackedBranch],
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,
    ) -> dict[str, gh.PullRequest]:
        """Find the PR (any state) for every branch in the component.

        Used by stack-block rendering, which wants to surface merged PRs
        with a "[merged]" label rather than dropping them silently.
        """
        out: dict[str, gh.PullRequest] = {}
        for node in component:
            pr = self._find_pr(node, config, current_repo)
            if pr is not None:
                out[node.branch] = pr
        return out

    def _find_open_pr(
        self,
        tracked: TrackedBranch,
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,
    ) -> gh.PullRequest | None:
        """Return the OPEN PR for `tracked`, or None.

        Callers use this to decide between create-vs-update: a closed
        or merged cached PR returns None here so a fresh PR is opened.
        """
        pr = self._find_pr(tracked, config, current_repo)
        return pr if pr is not None and pr.state == "OPEN" else None

    def _find_pr(
        self,
        tracked: TrackedBranch,
        config: RepoPRConfig,
        current_repo: gh.RepoInfo,  # noqa: ARG002 (interface parity with _find_open_pr)
    ) -> gh.PullRequest | None:
        """Return the PR for `tracked` in any state (open/closed/merged).

        Cache-first: if `tracked.pr_url` is set we trust it and hit the
        REST single-PR endpoint — no dependency on GitHub's search index.
        Matches universe gitstack's `StackItem.pr` caching model. Falls
        back to a GraphQL-style search (`repo:X head:Y is:pr is:open`)
        only when the cache is empty; writes the URL back on a hit so
        the next run is cache-only.
        """
        if tracked.pr_url:
            cached = self.pr_backend.view_pr(tracked.pr_url)
            if cached is not None:
                return cached
        target_repo = self._target_repo_slug(config)
        path = locate.locate_worktree(self.paths, tracked.repo_name, tracked.branch)
        if path is None:
            return None
        remote_branch = git.upstream_branch_name(path)
        if not remote_branch:
            return None
        # GraphQL search (not `gh pr list`): REST list misses cross-fork
        # same-owner PRs and was the source of the universe regression
        # where PR A was invisible to PR B's body rendering.
        query = f"repo:{target_repo} head:{remote_branch} is:pr is:open"
        prs = self.pr_backend.search_prs(query)
        if not prs:
            return None
        found = prs[0]
        # First-time discovery (e.g. PR opened by another tool): persist
        # the URL so subsequent runs skip the search entirely.
        if not tracked.pr_url:
            self._record_pr_url(tracked, found.url)
        return found

    def _record_pr_url(self, tracked: TrackedBranch, url: str) -> None:
        self.db.upsert_branch(
            TrackedBranch(
                repo_name=tracked.repo_name,
                branch=tracked.branch,
                parent_repo_name=tracked.parent_repo_name,
                parent_branch=tracked.parent_branch,
                managed_base_commit=tracked.managed_base_commit,
                last_synced_parent_commit=tracked.last_synced_parent_commit,
                last_clean_head=tracked.last_clean_head,
                pr_url=url,
            )
        )

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
        """Render the stack block embedded in a PR body.

        Matches universe gitstack's shape: a one-line preamble pointing
        reviewers at the current branch's files view, followed by an
        indented tree. Each branch renders as
        `[branch](pr) [[Files changed](files)]` with `[MERGED]` appended
        for landed PRs; the current branch's name is bolded inside the
        link.
        """
        lines = ["<!-- stacker:begin -->", "## Stacker", ""]
        # Preamble + Files-changed links only make sense when every PR bases
        # on trunk (repo-pr). In pr-pr mode GitHub's own base-chain already
        # gives reviewers the per-branch diff.
        if ctx.config.mode == "repo-pr":
            preamble = self._files_url(current_node, ctx)
            if preamble is not None:
                lines.append(
                    f"Use this [link]({preamble}) to review incremental changes."
                )
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
        is_current = node.branch == current_node.branch
        label = f"**{node.branch}**" if is_current else node.branch
        pr = ctx.pr_map.get(node.branch)
        if pr:
            parts = [f"[{label}]({pr.url})"]
            if ctx.config.mode == "repo-pr":
                files_url = self._files_url(node, ctx)
                if files_url:
                    parts.append(f"[[Files changed]({files_url})]")
            # Surface merged/closed PRs with a label, matching universe
            # gitstack's StackItem.merged rendering. Keeps history visible
            # instead of silently dropping landed PRs.
            if pr.state == "MERGED":
                parts.append("[MERGED]")
            elif pr.state == "CLOSED":
                parts.append("[CLOSED]")
            content = " ".join(parts)
        else:
            content = label
        lines = [f"{prefix}- {content}"]
        children = sorted(
            [child for child in ctx.component if child.parent_branch == node.branch],
            key=lambda item: item.branch,
        )
        for child in children:
            lines.extend(self._render_stack_lines(ctx, child, current_node, prefix=prefix + "  "))
        return lines

    def _files_url(self, node: TrackedBranch, ctx: _StackRender) -> str | None:
        """Build `<pr-url>/files/<base>..<head>` for reviewer navigation.

        Mirrors universe gitstack's per-PR files view
        (`<pr>/files/<parent_commit>..<head>`) — scoped to the PR so
        reviewers see only the commits unique to that branch. For a
        merged PR the range is dropped (`<pr>/files`) since the commit
        range no longer reflects reviewable changes. Uses
        `last_clean_head` (persisted by sync/init/repair), so ancestors
        and siblings render correctly even when not currently checked out.
        """
        pr = ctx.pr_map.get(node.branch)
        if pr is None:
            return None
        if pr.state == "MERGED":
            return f"{pr.url}/files"
        head_sha = node.last_clean_head
        if not head_sha:
            return None
        base_sha = node.managed_base_commit
        return (
            f"{pr.url}/files/"
            f"{quote(base_sha, safe=':/')}..{quote(head_sha, safe=':/')}"
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
                    return self._finish(logs, "Sync complete.")
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
                pr_url=tracked.pr_url,
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
        *,
        details: Details = "status-counts",
    ) -> None:
        label = selectors.selector_for(ctx.repo_name, branch)
        branch_label = self._style(label, fg="green", bold=not pos.implicit)
        suffixes = self._node_suffixes(ctx, branch, pos, details)
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
                details=details,
            )

    def _node_suffixes(
        self, ctx: _GraphCtx, branch: str, pos: _GraphPos, details: Details,
    ) -> list[str]:
        if details == "none":
            return []
        suffixes: list[str] = []
        tracked = next((item for item in ctx.items if item.branch == branch), None)
        if pos.implicit:
            suffixes.append(self._style("[untracked root]", fg="yellow"))
            return suffixes
        if tracked is None:
            return suffixes
        synced = self._is_synced(tracked)
        suffixes.append(
            self._style(
                "[synced]" if synced else "[unsynced]",
                fg="cyan" if synced else "yellow",
            )
        )
        if branch in ctx.live and self._has_graph_blocking_changes(ctx.live[branch].path):
            suffixes.append(self._style("[dirty]", fg="red", bold=True))
        if details in ("status-counts", "all"):
            count = self._count_branch_commits(tracked)
            if count is not None:
                suffixes.append(self._style(f"[{count} commits]", fg="white"))
        if details == "all" and tracked.pr_url:
            suffixes.append(self._style(f"[{tracked.pr_url}]", fg="magenta"))
        return suffixes

    def _count_branch_commits(self, tracked: TrackedBranch) -> int | None:
        path = locate.locate_worktree(self.paths, tracked.repo_name, tracked.branch)
        if path is None:
            return None
        with contextlib.suppress(git.GitError):
            return git.rev_count(path, f"{tracked.managed_base_commit}..HEAD")
        return None

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


def _drop_until(branches: list[TrackedBranch], name: str) -> list[TrackedBranch]:
    for index, item in enumerate(branches):
        if item.branch == name:
            return branches[index:]
    raise git.GitError(f"--from {name!r} is not in the resolved scope.")
