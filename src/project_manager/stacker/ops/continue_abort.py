from __future__ import annotations

from typing import TYPE_CHECKING

from project_manager.pool.db import PoolDB
from project_manager.stacker import git, ops_slot, selectors
from project_manager.stacker.cherry_pick import driver as cp_driver

from . import worktree

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def continue_operation(ctx: StackerCtx, repo_name: str) -> str:
    op = ctx.db.get_operation(repo_name)
    if not op:
        raise git.GitError("No paused stacker operation for this repo.")
    slot_path = worktree.slot_path_for_active_op(ctx, op)
    # Queue-advance state has op.branch=None; the driver will reacquire from
    # the queue on its own, so we leave the handle unset in that case.
    handle = (
        cp_driver.DriveHandle(
            slot_path=slot_path,
            acquired_ops=worktree.existing_ops_slot(ctx, op, slot_path),
        )
        if slot_path is not None
        else None
    )
    return cp_driver.run_until_pause_or_finish(
        ctx, repo_name, handle, continuing=True, logs=[],
    )


def abort_operation(ctx: StackerCtx, repo_name: str) -> str:
    op = ctx.db.get_operation(repo_name)
    if not op:
        raise git.GitError("No active stacker operation for this repo.")
    slot_path = worktree.slot_path_for_active_op(ctx, op, required=False)
    if slot_path and git.cherry_pick_in_progress(slot_path):
        git.cherry_pick_abort(slot_path)
    if slot_path and op.start_head:
        git.reset_hard(slot_path, op.start_head)
    ops = worktree.existing_ops_slot(ctx, op, slot_path) if slot_path else None
    ctx.db.clear_operation(repo_name)
    if ops is not None:
        ops_slot.release(PoolDB(ctx.paths.pool_db()), ops)
    if op.branch:
        return f"Aborted operation for {selectors.selector_for(repo_name, op.branch)}."
    return "Aborted operation."
