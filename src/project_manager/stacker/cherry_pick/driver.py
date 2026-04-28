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


# Final message emitted when a single-branch op walks off the end of its
# commit list cleanly. downstream_sync has its own completion path because it
# advances a queue rather than terminating on one branch.
_SINGLE_OP_DONE: dict[str, str] = {
    "local_sync": "Sync complete.",
    "local_absorb": "Absorb complete.",
}


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
    # Drop commits whose patch is already on the parent (mirrors `git rebase`'s
    # default). Keeps absorb→sync flows conflict-free and makes the parent the
    # source of truth whenever both sides carry the same logical change.
    commit_list = git.rev_list_picking(slot_path, parent_head, start_head)
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
    # Outer try/finally: a `KeyboardInterrupt` (or any unexpected raise)
    # mid-loop must still hand the in-flight slot back to the pool when
    # the worktree is clean. Without it, cancelling a downstream_sync
    # leaves the just-checked-out branch claimed as `stacker|ops`,
    # blocking the next stacker op until manual recovery.
    try:
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
                    _release_handle_if_clean(ctx, handle)
                    handle = None
                    return fmt.finish(ctx, logs, result)
                op = ctx.db.get_operation(repo_name)
                assert op
                handle = None
                continue
            single_op_done = _SINGLE_OP_DONE.get(op.op_type)
            if single_op_done is not None:
                ctx.db.clear_operation(repo_name)
                return fmt.finish(ctx, logs, single_op_done)
            if op.op_type == "downstream_sync":
                if op.current_index >= len(op.queue):
                    ctx.db.clear_operation(repo_name)
                    return fmt.finish(ctx, logs, "Sync complete.")
                handle = advance_downstream(ctx, repo_name, op, logs)
                op = ctx.db.get_operation(repo_name)
                assert op
                continue
            raise git.GitError(f"Unknown operation type {op.op_type}.")
    finally:
        _release_handle_if_clean(ctx, handle)


def _release_handle_if_clean(ctx: StackerCtx, handle: DriveHandle | None) -> None:
    """Hand `handle.acquired_ops` back to the pool when safe.

    The cherry-pick driver carries the active slot in the handle. We
    release it the same way `slot.release_if_clean` releases a fresh
    `AcquiredSlot`: skip when no claim is held, hold when the worktree
    has resumable state (uncommitted changes / `CHERRY_PICK_HEAD`),
    otherwise detach + release.
    """
    if handle is None or handle.acquired_ops is None:
        return
    if git.has_resumable_state(handle.slot_path):
        return
    ops_slot.release(PoolDB(ctx.paths.pool_db()), handle.acquired_ops)


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
    # Bridge the acquire → DriveHandle handoff: an interrupt or raise
    # between here and `return DriveHandle(...)` would orphan the
    # just-claimed slot since the caller's outer finally only sees
    # `handle = advance_downstream(...)` after this returns.
    try:
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
            worktree.release_if_clean(ctx, acquired)
            return None
        prepare_local_operation(
            ctx, tracked, op_type="downstream_sync", slot_path=acquired.path, logs=logs
        )
    except BaseException:
        worktree.release_if_clean(ctx, acquired)
        raise
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
    if op.op_type == "local_absorb":
        return _finalize_absorb(ctx, op, slot_path)
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


def _finalize_absorb(
    ctx: StackerCtx, op: OperationState, slot_path: Path
) -> str:
    """Absorb variant: parent advanced by child's commits; child untouched.

    Intentionally does NOT call `require_tracked` or `upsert_branch` with the
    sync-shaped payload — `op.branch` here is the *parent*, and its row (if
    tracked) must keep its own `managed_base_commit` / `parent_branch`
    pointing at its own lineage. We only advance the parent's
    `last_clean_head`; if the parent is trunk (no row), the git-ref update in
    the slot is the entire state change.
    """
    assert op.branch
    parent_new_head = git.rev_parse(slot_path, "HEAD")
    parent_row = ctx.db.get_branch(op.repo_name, op.branch)
    if parent_row is not None:
        ctx.db.upsert_branch(
            TrackedBranch(
                repo_name=parent_row.repo_name,
                branch=parent_row.branch,
                parent_repo_name=parent_row.parent_repo_name,
                parent_branch=parent_row.parent_branch,
                managed_base_commit=parent_row.managed_base_commit,
                last_synced_parent_commit=parent_row.last_synced_parent_commit,
                last_clean_head=parent_new_head,
            )
        )
    ctx.db.clear_operation(op.repo_name)
    parent_label = selectors.selector_for(op.repo_name, op.branch)
    return f"Absorb complete. {parent_label} at {fmt.short(parent_new_head)}."


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
    other_selector = selectors.selector_for(op.repo_name, op.parent_branch or "")
    # For sync, op.branch is the child being rebased and op.parent_branch is
    # where we're syncing onto. For absorb the mapping flips: op.branch is the
    # parent being updated, op.parent_branch is the source child. Headline
    # wording follows that.
    if op.op_type == "local_absorb":
        headline = (
            f"Absorb paused on {inspect_selector} while absorbing from "
            f"{other_selector}."
        )
    else:
        headline = (
            f"Sync paused on {inspect_selector} while syncing onto "
            f"{other_selector}."
        )
    cherry = git.cherry_pick_in_progress(slot_path)
    parts = [f"{headline}\nCherry-pick in progress: {'yes' if cherry else 'no'}"]
    if op.error_message:
        parts.append(f"Git says: {op.error_message}")
    parts.append(f"Worktree at: {slot_path}")
    parts.append("Next action: `pm stacker continue` or `pm stacker abort`")
    return "\n".join(parts)
