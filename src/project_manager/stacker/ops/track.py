from __future__ import annotations

from typing import TYPE_CHECKING

from project_manager.stacker import git, locate, selectors
from project_manager.stacker.models import ParentLocator, SelectorTarget, TrackedBranch

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def require_tracked(ctx: StackerCtx, target: SelectorTarget) -> TrackedBranch:
    tracked = ctx.db.get_branch(target.repo_name, target.branch)
    if not tracked:
        raise git.GitError(
            f"{selectors.selector_for(target.repo_name, target.branch)} is not tracked. "
            "Use `pm stacker create` to start a new stacked branch."
        )
    return tracked


def create_tracked_branch(
    ctx: StackerCtx,
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
    repo_path = ctx.paths.repo(repo_name)
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
    ctx.db.upsert_branch(tracked)
    return tracked


def track(
    ctx: StackerCtx, target: SelectorTarget, parent: ParentLocator
) -> TrackedBranch:
    if target.repo_name != parent.repo_name:
        raise git.GitError("Parent and child must be in the same repo.")
    repo_path = ctx.paths.repo(target.repo_name)
    base_commit = git.merge_base(repo_path, parent.branch, target.branch)
    parent_head = git.rev_parse(repo_path, parent.branch)
    branch_slot = locate.locate_worktree(ctx.paths, target.repo_name, target.branch)
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
    ctx.db.upsert_branch(tracked)
    return tracked
