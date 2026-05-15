from __future__ import annotations

from typing import TYPE_CHECKING

from project_manager.stacker import git, locate, selectors
from project_manager.stacker.models import SelectorTarget, TrackedBranch

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def remove(
    ctx: StackerCtx,
    target: SelectorTarget,
    *,
    keep_branch: bool = False,
    parent_cascade: bool = False,
    force: bool = False,  # noqa: ARG001 — reserved for future interactive-confirm gate
) -> str:
    """Stop tracking a branch; optionally delete it and/or cascade upward.

    Default: untrack, reparent children onto the removed branch's
    parent, and delete the underlying git branch. `keep_branch=True`
    preserves the git branch (matches the old `untrack` semantics).
    `parent_cascade=True` also removes every tracked ancestor chain
    above the target (children of those ancestors get reparented up).
    Refuses when an op is paused on this repo.
    """
    if ctx.db.get_operation(target.repo_name):
        raise git.GitError(
            "Another stacker operation is active for this repo. "
            "Use `pm stacker continue` or `pm stacker abort`."
        )
    tracked = ctx.db.get_branch(target.repo_name, target.branch)
    if not tracked:
        raise git.GitError(
            f"{selectors.selector_for(target.repo_name, target.branch)} is not tracked."
        )
    removed: list[str] = []
    cursor: TrackedBranch | None = tracked
    while cursor is not None:
        _remove_one(ctx, cursor, keep_branch=keep_branch)
        removed.append(selectors.selector_for(cursor.repo_name, cursor.branch))
        if not parent_cascade:
            break
        cursor = ctx.db.get_branch(cursor.parent_repo_name, cursor.parent_branch)
    if len(removed) == 1:
        return f"Removed {removed[0]}."
    return "Removed:\n  " + "\n  ".join(removed)


def _remove_one(ctx: StackerCtx, tracked: TrackedBranch, *, keep_branch: bool) -> None:
    """Untrack `tracked`, reparent children, optionally delete the git branch."""
    for child in ctx.db.get_children(tracked.repo_name, tracked.branch):
        ctx.db.upsert_branch(
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
    ctx.db.delete_branch(tracked.repo_name, tracked.branch)
    if keep_branch:
        return
    slot_path = locate.locate_worktree(ctx.paths, tracked.repo_name, tracked.branch)
    if slot_path is not None:
        # Detach so `git branch -D` won't refuse because it's checked out.
        git.detach_head(slot_path)
    repo_path = ctx.paths.repo(tracked.repo_name)
    git.git(repo_path, "branch", "-D", tracked.branch, check=False)
