from __future__ import annotations

from typing import TYPE_CHECKING

from project_manager.stacker import git
from project_manager.stacker.models import SelectorTarget, TrackedBranch

from . import worktree
from .track import require_tracked

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def rename(ctx: StackerCtx, target: SelectorTarget, new_name: str) -> str:
    """Rename the current branch; rewrite DB rows for it and its children.

    Does not touch the remote branch or PR head-ref: the next `push`
    will create the new remote branch; the old one lingers until
    cleaned up externally. `git branch -m` also drops the upstream
    config, so the next push must re-establish it.
    """
    if ctx.db.get_operation(target.repo_name):
        raise git.GitError(
            "Another stacker operation is active for this repo. "
            "Use 'stacker continue' or 'stacker abort'."
        )
    tracked = require_tracked(ctx, target)
    if target.branch == new_name:
        return f"Branch already named {new_name}."
    repo_path = ctx.paths.repo(tracked.repo_name)
    if git.branch_exists(repo_path, new_name):
        raise git.GitError(f"Branch {new_name} already exists.")
    current_path = worktree.require_checked_out(
        ctx, tracked.repo_name, tracked.branch
    )
    children = ctx.db.get_children(tracked.repo_name, target.branch)
    git.git(current_path, "branch", "-m", target.branch, new_name)
    ctx.db.delete_branch(tracked.repo_name, target.branch)
    ctx.db.upsert_branch(
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
        ctx.db.upsert_branch(
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
