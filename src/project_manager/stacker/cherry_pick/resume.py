from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from project_manager.stacker import git, selectors
from project_manager.stacker.models import OperationState
from project_manager.stacker.render import format as fmt

from . import driver, empty

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def resume_cherry_pick(
    ctx: StackerCtx,
    repo_name: str,
    op: OperationState,
    slot_path: Path,
    logs: list[str],
) -> str | None:
    assert op.branch
    op.next_commit_index = driver.recompute_progress(slot_path, op)
    if git.cherry_pick_in_progress(slot_path):
        label = selectors.selector_for(repo_name, op.branch)
        fmt.record(ctx, logs, f"Continuing cherry-pick in {label}")
        proc = git.cherry_pick_continue(slot_path)
        if proc.returncode != 0:
            message = (
                proc.stderr.strip()
                or proc.stdout.strip()
                or "cherry-pick --continue failed"
            )
            if empty.is_empty_cherry_pick_message(message):
                return resume_skip_empty(ctx, op, slot_path, logs, label)
            op.status = "paused"
            op.error_message = message
            ctx.db.put_operation(op)
            return driver.failure_message(op, slot_path)
        op.next_commit_index += 1
        ctx.db.put_operation(op)
    elif empty.should_skip_empty_commit(op):
        commit = op.commit_list[op.next_commit_index]
        fmt.record(
            ctx,
            logs,
            f"Skipping empty cherry-pick {fmt.short(commit)} on "
            f"{selectors.selector_for(repo_name, op.branch)}",
        )
        op.next_commit_index += 1
        op.error_message = None
        op.status = "running"
        ctx.db.put_operation(op)
    return None


def resume_skip_empty(
    ctx: StackerCtx,
    op: OperationState,
    slot_path: Path,
    logs: list[str],
    label: str,
) -> str | None:
    commit = op.commit_list[op.next_commit_index]
    skip = git.cherry_pick_skip(slot_path)
    if skip.returncode != 0:
        op.status = "paused"
        op.error_message = (
            skip.stderr.strip() or skip.stdout.strip() or "cherry-pick --skip failed"
        )
        ctx.db.put_operation(op)
        return driver.failure_message(op, slot_path)
    fmt.record(ctx, logs, f"Skipping empty cherry-pick {fmt.short(commit)} on {label}")
    op.next_commit_index += 1
    op.error_message = None
    op.status = "running"
    ctx.db.put_operation(op)
    return None


def cherry_pick_remaining(
    ctx: StackerCtx,
    repo_name: str,
    op: OperationState,
    slot_path: Path,
    logs: list[str],
) -> str | None:
    assert op.branch
    while op.next_commit_index < len(op.commit_list):
        commit = op.commit_list[op.next_commit_index]
        fmt.record(
            ctx,
            logs,
            f"Cherry-picking {fmt.short(commit)} onto "
            f"{selectors.selector_for(repo_name, op.branch)}",
        )
        proc = git.cherry_pick(slot_path, commit)
        if proc.returncode != 0:
            op.status = "paused"
            op.error_message = (
                proc.stderr.strip()
                or proc.stdout.strip()
                or f"cherry-pick failed for {commit}"
            )
            ctx.db.put_operation(op)
            return driver.failure_message(op, slot_path)
        op.next_commit_index += 1
        ctx.db.put_operation(op)
    return None
