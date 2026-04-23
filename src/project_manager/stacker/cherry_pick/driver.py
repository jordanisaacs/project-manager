from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from project_manager.pool import slot as slot_mod
from project_manager.pool.db import PoolDB
from project_manager.stacker import git, ops_slot, selectors
from project_manager.stacker.models import OperationState, SelectorTarget, TrackedBranch
from project_manager.stacker.ops import worktree
from project_manager.stacker.ops.track import require_tracked
from project_manager.stacker.render import format as fmt

from . import resume

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


@dataclass(frozen=True)
class DriveHandle:
    """Worktree + pool slot currently bound to an in-flight op.

    `slot_path` is where git commands run. `acquired_ops` is the pool
    slot this stacker invocation claimed (and must release on clean
    completion); None when we reused an existing project-owned slot.
    """

    slot_path: Path
    acquired_ops: slot_mod.Slot | None


def sync_plan(
    ctx: StackerCtx, tracked: TrackedBranch, slot_path: Path
) -> tuple[str, str, list[str]]:
    repo_path = ctx.paths.repo(tracked.parent_repo_name)
    parent_head = git.rev_parse(repo_path, tracked.parent_branch)
    start_head = git.rev_parse(slot_path, "HEAD")
    commit_list = git.rev_list(
        slot_path, f"{tracked.managed_base_commit}..{start_head}"
    )
    return parent_head, start_head, commit_list


def prepare_local_operation(
    ctx: StackerCtx,
    tracked: TrackedBranch,
    *,
    op_type: str,
    slot_path: Path,
    logs: list[str],
) -> OperationState:
    parent_head, start_head, commit_list = sync_plan(ctx, tracked, slot_path)
    op = ctx.db.get_operation(tracked.repo_name)
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
    ctx.db.put_operation(op)
    fmt.record(
        ctx,
        logs,
        f"Resetting {selectors.selector_for(tracked.repo_name, tracked.branch)} to "
        f"{selectors.selector_for(tracked.parent_repo_name, tracked.parent_branch)} "
        f"({fmt.short(parent_head)})",
    )
    git.reset_hard(slot_path, parent_head)
    return op


def run_until_pause_or_finish(
    ctx: StackerCtx,
    repo_name: str,
    handle: DriveHandle | None = None,
    *,
    continuing: bool = False,
    logs: list[str] | None = None,
) -> str:
    if logs is None:
        logs = []
    op = ctx.db.get_operation(repo_name)
    assert op
    while True:
        if op.branch:
            if handle is None:
                slot_path = worktree.require_checked_out(ctx, repo_name, op.branch)
                handle = DriveHandle(
                    slot_path=slot_path,
                    acquired_ops=worktree.existing_ops_slot(ctx, op, slot_path),
                )
            result = drive_local(
                ctx, op, slot_path=handle.slot_path, continuing=continuing, logs=logs
            )
            continuing = False
            if result is not None:
                if handle.acquired_ops is not None:
                    ops_slot.release(PoolDB(ctx.paths.pool_db()), handle.acquired_ops)
                return fmt.finish(ctx, logs, result)
            op = ctx.db.get_operation(repo_name)
            assert op
            handle = None
            continue
        if op.op_type == "local_sync":
            ctx.db.clear_operation(repo_name)
            return fmt.finish(ctx, logs, "Sync complete.")
        if op.op_type == "downstream_sync":
            if op.current_index >= len(op.queue):
                ctx.db.clear_operation(repo_name)
                return fmt.finish(ctx, logs, "Sync complete.")
            handle = advance_downstream(ctx, repo_name, op, logs)
            op = ctx.db.get_operation(repo_name)
            assert op
            continue
        raise git.GitError(f"Unknown operation type {op.op_type}.")


def advance_downstream(
    ctx: StackerCtx,
    repo_name: str,
    op: OperationState,
    logs: list[str],
) -> DriveHandle | None:
    """Move the downstream_sync queue forward by one entry.

    Returns a DriveHandle for the newly in-flight local op, or None if
    the entry was skipped because the parent was unchanged.
    """
    next_branch = op.queue[op.current_index]
    tracked = ctx.db.get_branch(repo_name, next_branch)
    if not tracked:
        raise git.GitError(
            f"Tracked branch disappeared from state: "
            f"{selectors.selector_for(repo_name, next_branch)}"
        )
    acquired = worktree.acquire(ctx, repo_name, next_branch)
    parent_head, _, _ = sync_plan(ctx, tracked, acquired.path)
    if parent_head == tracked.managed_base_commit:
        child_label = selectors.selector_for(tracked.repo_name, tracked.branch)
        parent_label = selectors.selector_for(
            tracked.parent_repo_name, tracked.parent_branch
        )
        fmt.record(
            ctx,
            logs,
            f"Skipping {child_label}; "
            f"parent {parent_label} is unchanged at {fmt.short(parent_head)}",
        )
        op.current_index += 1
        ctx.db.put_operation(op)
        worktree.release_if_owned(ctx, acquired)
        return None
    prepare_local_operation(
        ctx, tracked, op_type="downstream_sync", slot_path=acquired.path, logs=logs
    )
    return DriveHandle(slot_path=acquired.path, acquired_ops=acquired.ops)


def drive_local(
    ctx: StackerCtx,
    op: OperationState,
    *,
    slot_path: Path,
    continuing: bool,
    logs: list[str],
) -> str | None:
    assert op.branch
    if continuing:
        result = resume.resume_cherry_pick(ctx, op.repo_name, op, slot_path, logs)
        if result is not None:
            return result
    pause = resume.cherry_pick_remaining(ctx, op.repo_name, op, slot_path, logs)
    if pause is not None:
        return pause
    return finalize_local_op(ctx, op, slot_path)


def finalize_local_op(
    ctx: StackerCtx, op: OperationState, slot_path: Path
) -> str | None:
    assert op.branch
    tracked = require_tracked(
        ctx, SelectorTarget(repo_name=op.repo_name, branch=op.branch)
    )
    ctx.db.upsert_branch(
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
        ctx.db.clear_operation(op.repo_name)
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
    ctx.db.put_operation(op)
    return None


def recompute_progress(path: Path, op: OperationState) -> int:
    if not op.target_parent_head:
        return op.next_commit_index
    current_head = git.rev_parse(path, "HEAD")
    if current_head == op.target_parent_head:
        return 0
    applied_count = git.rev_count(path, f"{op.target_parent_head}..{current_head}")
    if applied_count < 0:
        return 0
    return min(applied_count, len(op.commit_list))


def failure_message(op: OperationState, slot_path: Path) -> str:
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
