from __future__ import annotations

from typing import TYPE_CHECKING

from project_manager.stacker import git
from project_manager.stacker.models import (
    DEFAULT_SYNC_OPTIONS,
    ParentLocator,
    ScopeSpec,
    SelectorTarget,
    SyncOptions,
    TrackedBranch,
)
from project_manager.stacker.pr.lineage import toposorted_descendants

from . import sync as sync_ops
from .track import require_tracked

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def reparent(
    ctx: StackerCtx, target: SelectorTarget, new_parent: ParentLocator,
    *, options: SyncOptions = DEFAULT_SYNC_OPTIONS,
) -> str:
    """Move `target` onto a new parent; cascade cherry-pick through descendants.

    DB-only rewrite of `parent_branch`; `managed_base_commit` stays
    at the old parent's tip so the next sync sees every commit from
    `old_base..HEAD` as "to cherry-pick onto the new parent". Sync
    then updates managed_base to the new parent's tip on finalize.

    `options` is forwarded to the downstream sync — see
    `pm stacker sync` for semantics.
    """
    if ctx.db.get_operation(target.repo_name):
        raise git.GitError(
            "Another stacker operation is active for this repo. "
            "Use `pm stacker continue` or `pm stacker abort`."
        )
    if new_parent.repo_name != target.repo_name:
        raise git.GitError("Cross-repo reparent is not supported.")
    tracked = require_tracked(ctx, target)
    if new_parent.branch == tracked.branch:
        raise git.GitError(f"{tracked.branch} cannot be its own parent.")
    if _is_descendant(ctx, tracked, new_parent.branch):
        raise git.GitError(
            f"{new_parent.branch} is a descendant of {tracked.branch}; "
            "reparenting would create a cycle."
        )
    repo_path = ctx.paths.repo(tracked.repo_name)
    if not git.branch_exists(repo_path, new_parent.branch):
        raise git.GitError(
            f"Branch '{new_parent.branch}' not found in {tracked.repo_name}."
        )
    ctx.db.upsert_branch(
        TrackedBranch(
            repo_name=tracked.repo_name,
            branch=tracked.branch,
            parent_repo_name=new_parent.repo_name,
            parent_branch=new_parent.branch,
            managed_base_commit=tracked.managed_base_commit,
            last_synced_parent_commit=tracked.last_synced_parent_commit,
            last_clean_head=tracked.last_clean_head,
        )
    )
    return sync_ops.sync(
        ctx, target, ScopeSpec(scope="current", skip_ancestors=True),
        options=options,
    )


def _is_descendant(
    ctx: StackerCtx, tracked: TrackedBranch, candidate: str
) -> bool:
    for descendant in toposorted_descendants(ctx, tracked.repo_name, tracked.branch):
        if descendant.branch == candidate:
            return True
    return False
