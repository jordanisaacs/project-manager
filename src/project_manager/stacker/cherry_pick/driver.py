from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from project_manager.stacker import git, selectors, slot
from project_manager.stacker.models import (
    OperationState,
    SelectorTarget,
    SyncOptions,
    TrackedBranch,
)
from project_manager.stacker.ops import sync_gates, worktree
from project_manager.stacker.ops.track import require_tracked
from project_manager.stacker.render import format as fmt
from project_manager.stacker.slot import AcquiredSlot

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


def sync_plan(
    ctx: StackerCtx,
    tracked: TrackedBranch,
    slot_path: Path,
) -> tuple[str, str, list[str]]:
    """Plan an exact-range cherry-pick of the branch's working commits.

    The commit list is always `managed_base..HEAD` — only the commits
    the child added on top of its recorded base. Patch-id dedup against
    the parent is gone: callers can't tell when an in-place parent
    rewrite changes the patch of a "shared" commit, so the dedup path
    silently replayed superseded duplicates. Pre-flight gates in
    `ops.sync_gates` catch the cases this used to mishandle.
    """
    repo_path = ctx.paths.repo(tracked.parent_repo_name)
    parent_head = git.rev_parse(repo_path, tracked.parent_branch)
    start_head = git.rev_parse(slot_path, "HEAD")
    commit_list = git.rev_list(slot_path, f"{tracked.managed_base_commit}..{start_head}")
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
        op = OperationState(repo_name=tracked.repo_name, op_type=op_type, status="running")
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
    handle: AcquiredSlot | None = None,
    *,
    continuing: bool = False,
    logs: list[str] | None = None,
) -> str:
    if logs is None:
        logs = []
    op = ctx.db.get_operation(repo_name)
    assert op
    # Outer try/finally is the safety net for the gap between
    # `advance_downstream` returning a fresh slot and the next
    # iteration's `_owned_slot` `with`-block taking ownership. Inside
    # the `with`, the context manager releases the slot on success,
    # exception, and `KeyboardInterrupt`.
    try:
        while True:
            if op.branch:
                with _owned_slot(ctx, repo_name, op, handle) as h:
                    result = drive_local(
                        ctx,
                        op,
                        slot_path=h.path,
                        continuing=continuing,
                        logs=logs,
                    )
                    continuing = False
                handle = None
                if result is not None:
                    return fmt.finish(ctx, logs, result)
                op = ctx.db.get_operation(repo_name)
                assert op
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
        if handle is not None:
            slot.release_if_clean(ctx, handle)


@contextlib.contextmanager
def _owned_slot(
    ctx: StackerCtx,
    repo_name: str,
    op: OperationState,
    existing: AcquiredSlot | None,
) -> Iterator[AcquiredSlot]:
    """Yield an `AcquiredSlot` for `op.branch`; `release_if_clean` on exit.

    Three slot origins funnel through here uniformly:
      - Caller passed one in (e.g. `_sync_one` after `acquired_for_op`).
      - Loop reconstructs it from the live worktree for an op resumed
        from disk (`require_checked_out` + `existing_ops_slot`).
      - `advance_downstream` minted one for the next queue entry.
    Cleanup is the shared `slot.release_if_clean` so a paused cherry-pick
    (CHERRY_PICK_HEAD set) keeps its slot for `pm stacker continue`,
    and a clean exit returns it to the pool. Caller-passed slots are
    released here too — that's idempotent against the caller's own
    `acquired_for_op`/`release_if_clean`, both of which no-op once the
    pool row is gone.
    """
    assert op.branch is not None
    if existing is not None:
        owned = existing
    else:
        slot_path = worktree.require_checked_out(ctx, repo_name, op.branch)
        owned = AcquiredSlot(
            path=slot_path,
            ops=worktree.existing_ops_slot(ctx, op, slot_path),
        )
    try:
        yield owned
    finally:
        slot.release_if_clean(ctx, owned)


def advance_downstream(
    ctx: StackerCtx,
    repo_name: str,
    op: OperationState,
    logs: list[str],
) -> AcquiredSlot | None:
    """Move the downstream_sync queue forward by one entry.

    Returns the freshly-acquired `AcquiredSlot` for the newly in-flight
    local op, or None if the entry was skipped because the parent was
    unchanged. The slot becomes the loop's in-flight handle and is
    released by `_owned_slot`'s `with`-block.
    """
    next_branch = op.queue[op.current_index]
    tracked = ctx.db.get_branch(repo_name, next_branch)
    if not tracked:
        raise git.GitError(
            f"Tracked branch disappeared from state: "
            f"{selectors.selector_for(repo_name, next_branch)}"
        )
    acquired = worktree.acquire(ctx, repo_name, next_branch)
    # Bridge the acquire → return handoff: an interrupt or raise here
    # would orphan the just-claimed slot, since the caller's outer
    # finally only sees the new slot after this returns.
    try:
        options = SyncOptions(
            allow_drop_parent_modifications=op.allow_drop_parent_modifications,
            allow_drop_merge=op.allow_drop_merge,
        )
        try:
            sync_gates.run_branch_gates(ctx, tracked, acquired.path, options=options)
        except git.GitError:
            # Gate failure aborts the multi-branch queue but doesn't leave
            # a half-built operation row behind — there's nothing to
            # `continue` once the gate has refused, so clear state so the
            # user can re-run with the matching `--allow-drop-*` flag.
            ctx.db.clear_operation(repo_name)
            raise
        collapse_msg = sync_gates.collapse_if_merged(
            ctx,
            tracked,
            acquired.path,
            allow_drop_merge=op.allow_drop_merge,
        )
        if collapse_msg is not None:
            fmt.record(ctx, logs, collapse_msg)
            op.current_index += 1
            ctx.db.put_operation(op)
            worktree.release_if_clean(ctx, acquired)
            return None
        parent_head, _, _ = sync_plan(ctx, tracked, acquired.path)
        if parent_head == tracked.managed_base_commit:
            child_label = selectors.selector_for(tracked.repo_name, tracked.branch)
            parent_label = selectors.selector_for(tracked.parent_repo_name, tracked.parent_branch)
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
            ctx,
            tracked,
            op_type="downstream_sync",
            slot_path=acquired.path,
            logs=logs,
        )
    except BaseException:
        worktree.release_if_clean(ctx, acquired)
        raise
    return acquired


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


def finalize_local_op(ctx: StackerCtx, op: OperationState, slot_path: Path) -> str | None:
    assert op.branch
    if op.op_type == "local_absorb":
        return _finalize_absorb(ctx, op, slot_path)
    tracked = require_tracked(ctx, SelectorTarget(repo_name=op.repo_name, branch=op.branch))
    ctx.db.upsert_branch(
        TrackedBranch(
            repo_name=tracked.repo_name,
            branch=tracked.branch,
            parent_repo_name=tracked.parent_repo_name,
            parent_branch=tracked.parent_branch,
            managed_base_commit=op.target_parent_head or tracked.managed_base_commit,
            last_synced_parent_commit=(op.target_parent_head or tracked.last_synced_parent_commit),
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


def _finalize_absorb(ctx: StackerCtx, op: OperationState, slot_path: Path) -> str:
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
        headline = f"Absorb paused on {inspect_selector} while absorbing from {other_selector}."
    else:
        headline = f"Sync paused on {inspect_selector} while syncing onto {other_selector}."
    cherry = git.cherry_pick_in_progress(slot_path)
    parts = [f"{headline}\nCherry-pick in progress: {'yes' if cherry else 'no'}"]
    if op.error_message:
        parts.append(f"Git says: {op.error_message}")
    parts.append(f"Worktree at: {slot_path}")
    parts.append("Next action: `pm stacker continue` or `pm stacker abort`")
    return "\n".join(parts)
