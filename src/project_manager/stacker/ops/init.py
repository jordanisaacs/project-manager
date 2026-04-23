from __future__ import annotations

from typing import TYPE_CHECKING

from project_manager.stacker import git
from project_manager.stacker.models import TrackedBranch, WorktreeInit

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def init_new_branch(ctx: StackerCtx, spec: WorktreeInit) -> TrackedBranch:
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
    repo_path = ctx.paths.repo(spec.repo_name)
    parent_head = git.rev_parse(repo_path, spec.parent.branch)
    start_point = (
        git.rev_parse(repo_path, spec.copy_from) if spec.copy_from else parent_head
    )
    git.git(spec.worktree_path, "checkout", "-b", spec.branch, start_point)
    return _persist_init(ctx, spec, parent_head, parent_head)


def init_adopt_branch(ctx: StackerCtx, spec: WorktreeInit) -> TrackedBranch | None:
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
    repo_path = ctx.paths.repo(spec.repo_name)
    managed_base = git.merge_base(repo_path, spec.parent.branch, spec.branch)
    return _persist_init(ctx, spec, managed_base, None)


def _persist_init(
    ctx: StackerCtx,
    spec: WorktreeInit,
    managed_base: str,
    last_synced_override: str | None,
) -> TrackedBranch:
    assert spec.parent is not None
    repo_path = ctx.paths.repo(spec.repo_name)
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
    ctx.db.upsert_branch(tracked)
    return tracked
