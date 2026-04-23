from __future__ import annotations

from typing import TYPE_CHECKING

from project_manager.stacker import git, selectors
from project_manager.stacker.cherry_pick import driver as cp_driver
from project_manager.stacker.models import (
    DEFAULT_SCOPE,
    OperationState,
    ScopeSpec,
    SelectorTarget,
    TrackedBranch,
)
from project_manager.stacker.pr.lineage import resolve_scope
from project_manager.stacker.render import format as fmt
from project_manager.stacker.render.graph import ensure_syncable

from . import worktree
from .track import require_tracked

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def sync(
    ctx: StackerCtx, target: SelectorTarget, spec: ScopeSpec = DEFAULT_SCOPE
) -> str:
    """Cherry-pick a set of tracked branches onto their parents.

    With a single-branch resolution we take the local_sync fast path so
    callers see the "Nothing to sync" short-circuit; with a multi-branch
    resolution we drive a downstream_sync queue that syncs each entry in
    parent-before-child order.
    """
    if ctx.db.get_operation(target.repo_name):
        raise git.GitError(
            "Another stacker operation is active for this repo. "
            "Use 'stacker continue' or 'stacker abort'."
        )
    resolved = resolve_scope(ctx, target.repo_name, target.branch, spec)
    if not resolved:
        return "No tracked branches to sync."
    if len(resolved) == 1:
        return _sync_one(ctx, resolved[0])
    ctx.db.put_operation(
        OperationState(
            repo_name=target.repo_name,
            op_type="downstream_sync",
            status="running",
            root_branch=resolved[0].branch,
            queue=[b.branch for b in resolved],
            current_index=0,
        )
    )
    return cp_driver.run_until_pause_or_finish(ctx, target.repo_name, logs=[])


def _sync_one(ctx: StackerCtx, tracked: TrackedBranch) -> str:
    """Single-branch sync path: local_sync op with up-to-date short-circuit."""
    ctx.db.put_operation(
        OperationState(
            repo_name=tracked.repo_name,
            op_type="local_sync",
            status="running",
            branch=tracked.branch,
            parent_branch=tracked.parent_branch,
        )
    )
    try:
        acquired = worktree.acquire(ctx, tracked.repo_name, tracked.branch)
    except Exception:
        ctx.db.clear_operation(tracked.repo_name)
        raise
    parent_head, _, _ = cp_driver.sync_plan(ctx, tracked, acquired.path)
    if parent_head == tracked.managed_base_commit:
        ctx.db.clear_operation(tracked.repo_name)
        worktree.release_if_owned(ctx, acquired)
        child_label = selectors.selector_for(tracked.repo_name, tracked.branch)
        parent_label = selectors.selector_for(
            tracked.parent_repo_name, tracked.parent_branch
        )
        return (
            f"Nothing to sync for {child_label}.\n"
            f"Parent {parent_label} is unchanged at {fmt.short(parent_head)}."
        )
    try:
        ensure_syncable(acquired.path)
    except git.GitError:
        ctx.db.clear_operation(tracked.repo_name)
        worktree.release_if_owned(ctx, acquired)
        raise
    logs: list[str] = []
    cp_driver.prepare_local_operation(
        ctx, tracked, op_type="local_sync", slot_path=acquired.path, logs=logs
    )
    return cp_driver.run_until_pause_or_finish(
        ctx,
        tracked.repo_name,
        cp_driver.DriveHandle(slot_path=acquired.path, acquired_ops=acquired.ops),
        logs=logs,
    )


def repair(ctx: StackerCtx, target: SelectorTarget, base_ref: str) -> str:
    tracked = require_tracked(ctx, target)
    if ctx.db.get_operation(target.repo_name):
        raise git.GitError(
            "Another stacker operation is active for this repo. "
            "Use 'stacker continue' or 'stacker abort'."
        )
    path = worktree.require_checked_out(ctx, target.repo_name, target.branch)
    ensure_syncable(path)
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
            f"Stored base already matches {fmt.short(actual_base)}."
        )
    ctx.db.upsert_branch(
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
        f"Stored base ref: {base_ref} -> {fmt.short(actual_base)}\n"
        f"Managed base: {fmt.short(tracked.managed_base_commit)} -> "
        f"{fmt.short(actual_base)}"
    )
