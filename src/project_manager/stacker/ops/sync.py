from __future__ import annotations

from typing import TYPE_CHECKING

from project_manager.stacker import git, locate, selectors
from project_manager.stacker.cherry_pick import driver as cp_driver
from project_manager.stacker.models import (
    DEFAULT_SCOPE,
    DEFAULT_SYNC_OPTIONS,
    OperationState,
    ScopeSpec,
    SelectorTarget,
    SyncOptions,
    TrackedBranch,
)
from project_manager.stacker.pr.lineage import resolve_scope
from project_manager.stacker.pr.refresh import refresh_review_state
from project_manager.stacker.render import format as fmt
from project_manager.stacker.render.graph import ensure_syncable

from . import sync_gates, worktree
from .track import require_tracked

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def sync(
    ctx: StackerCtx,
    target: SelectorTarget,
    spec: ScopeSpec = DEFAULT_SCOPE,
    *,
    options: SyncOptions = DEFAULT_SYNC_OPTIONS,
) -> str:
    """Cherry-pick a set of tracked branches onto their parents.

    With a single-branch resolution we take the local_sync fast path so
    callers see the "Nothing to sync" short-circuit; with a multi-branch
    resolution we drive a downstream_sync queue that syncs each entry in
    parent-before-child order.

    Unless `options.offline` is set, the PR state cache is refreshed
    once up front via one batched GraphQL call per (owner, repo) —
    per-branch gate decisions then read freshly-cached `pr_state` rows
    without further network calls.
    """
    _ensure_no_operation(ctx, target.repo_name)
    resolved = resolve_scope(ctx, target.repo_name, target.branch, spec)
    if not resolved:
        return "No tracked branches to sync."
    if not options.offline:
        refresh_review_state(ctx, resolved)
    if len(resolved) == 1:
        return _sync_one(ctx, resolved[0], options=options)
    _start_operation(
        ctx,
        OperationState(
            repo_name=target.repo_name,
            op_type="downstream_sync",
            status="running",
            root_branch=resolved[0].branch,
            queue=[b.branch for b in resolved],
            current_index=0,
            allow_drop_parent_modifications=options.allow_drop_parent_modifications,
            allow_drop_merge=options.allow_drop_merge,
        ),
    )
    return cp_driver.run_until_pause_or_finish(ctx, target.repo_name, logs=[])


def _sync_one(
    ctx: StackerCtx,
    tracked: TrackedBranch,
    *,
    options: SyncOptions,
) -> str:
    """Single-branch sync path: local_sync op with up-to-date short-circuit."""
    _start_operation(
        ctx,
        OperationState(
            repo_name=tracked.repo_name,
            op_type="local_sync",
            status="running",
            branch=tracked.branch,
            parent_branch=tracked.parent_branch,
            allow_drop_parent_modifications=options.allow_drop_parent_modifications,
            allow_drop_merge=options.allow_drop_merge,
        ),
    )
    # The context manager releases the slot iff the worktree is clean on
    # exit — covers success, exception, and `KeyboardInterrupt` in one
    # place. A pause-on-conflict mid-cherry-pick keeps the slot (predicate
    # sees `CHERRY_PICK_HEAD`) so `pm stacker continue` can resume.
    try:
        with worktree.acquired_for_sync(ctx, tracked.repo_name, tracked.branch) as acquired:
            sync_gates.run_branch_gates(ctx, tracked, acquired.path, options=options)
            collapse_msg = sync_gates.collapse_if_merged(
                ctx,
                tracked,
                acquired.path,
                allow_drop_merge=options.allow_drop_merge,
            )
            if collapse_msg is not None:
                ctx.db.clear_operation(tracked.repo_name)
                return collapse_msg
            parent_head, _, _ = cp_driver.sync_plan(ctx, tracked, acquired.path)
            if parent_head == tracked.managed_base_commit:
                ctx.db.clear_operation(tracked.repo_name)
                child_label = selectors.selector_for(tracked.repo_name, tracked.branch)
                parent_label = selectors.selector_for(
                    tracked.parent_repo_name, tracked.parent_branch
                )
                return (
                    f"Nothing to sync for {child_label}.\n"
                    f"Parent {parent_label} is unchanged at {fmt.short(parent_head)}."
                )
            # No `ensure_syncable` here: `run_branch_gates` above already
            # ran it as its first, unconditional check, and the collapse
            # path only ever resets to a clean parent tip.
            logs: list[str] = []
            cp_driver.prepare_local_operation(
                ctx,
                tracked,
                op_type="local_sync",
                slot_path=acquired.path,
                logs=logs,
            )
            return cp_driver.run_until_pause_or_finish(
                ctx,
                tracked.repo_name,
                acquired,
                logs=logs,
            )
    except BaseException:
        # The slot is already handled by the context manager above; we
        # just need to clear the OperationState so a stale row doesn't
        # block the next stacker invocation in this repo.
        if ctx.db.get_operation(tracked.repo_name):
            ctx.db.clear_operation(tracked.repo_name)
        raise


def repair(ctx: StackerCtx, target: SelectorTarget, base_ref: str) -> str:
    tracked = require_tracked(ctx, target)
    if ctx.db.get_operation(target.repo_name):
        raise git.GitError(
            "Another stacker operation is active for this repo. "
            "Use `pm stacker continue` or `pm stacker abort`."
        )
    checked_out = locate.locate_worktree(ctx.paths, target.repo_name, target.branch)
    if checked_out is not None:
        # Do not record a dirty worktree as the branch's last-clean state.
        ensure_syncable(checked_out)
    repo_path = ctx.paths.repo(target.repo_name)
    actual_base = git.rev_parse(repo_path, _repair_ref(target.branch, base_ref))
    current_head = git.rev_parse(repo_path, target.branch)
    label = selectors.selector_for(tracked.repo_name, tracked.branch)
    if (
        actual_base == tracked.managed_base_commit
        and actual_base == (tracked.last_synced_parent_commit or "")
        and current_head == (tracked.last_clean_head or "")
    ):
        return (
            f"No repair needed for {label}.\nStored base already matches {fmt.short(actual_base)}."
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


def _ensure_no_operation(ctx: StackerCtx, repo_name: str) -> None:
    if ctx.db.get_operation(repo_name) is not None:
        _raise_operation_active()


def _start_operation(ctx: StackerCtx, op: OperationState) -> None:
    if not ctx.db.try_put_operation(op):
        _raise_operation_active()


def _raise_operation_active() -> None:
    raise git.GitError(
        "Another stacker operation is active for this repo. "
        "Use `pm stacker continue` or `pm stacker abort`."
    )


def _repair_ref(branch: str, base_ref: str) -> str:
    """Resolve checkout-relative shorthand against an explicitly targeted branch."""
    if base_ref == "HEAD" or base_ref.startswith(("HEAD~", "HEAD^", "HEAD@{", "HEAD:")):
        return f"{branch}{base_ref.removeprefix('HEAD')}"
    if base_ref.startswith("@{"):
        return f"{branch}{base_ref}"
    return base_ref
