from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OWNER_STACKER_OPS, OwnerKind, PoolDB
from project_manager.stacker import git, locate, ops_slot, selectors, slot
from project_manager.stacker.models import OperationState, TrackedBranch
from project_manager.stacker.render import format as fmt
from project_manager.stacker.slot import AcquiredSlot

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


# Back-compat alias: existing callers `from project_manager.stacker.ops import
# worktree` and reference `worktree._Acquired`. Keep the name pointing at the
# unified type so type annotations and isinstance checks still work.
_Acquired = AcquiredSlot


def acquire(ctx: StackerCtx, repo_name: str, branch: str) -> AcquiredSlot:
    """Resolve a worktree for generic push/absorb/etc. operations.

    Cwd reuse only kicks in when the cwd slot is detached. Sync has a
    separate safe-reuse policy in ``acquire_for_sync``.
    """
    return slot.resolve_slot(ctx, repo_name, branch)


def acquire_for_sync(ctx: StackerCtx, repo_name: str, branch: str) -> AcquiredSlot:
    """Resolve a safe checkout for one sync queue entry.

    Sync has a stronger reuse policy than the generic stacker resolver:

    1. Reuse an exact checkout only when it is a clean PM pool worktree and
       is not reserved by another stacker operation.
    2. Otherwise borrow the clean current PM worktree for this repo, even if
       doing so requires switching its branch. This check happens before a
       pool claim so a full pool cannot strand a downstream sync behind an
       unrelated stacker slot.
    3. Claim or wait for an ops slot only when neither checkout is safe.

    Project-owned slots keep their project owner throughout the operation.
    A free pool slot is claimed atomically before reuse so attach/stacker
    races cannot mutate the same checkout concurrently. Paused syncs retain
    stacker claims through the normal ``release_if_clean`` predicate; a
    project-owned checkout remains discoverable by its in-flight branch.
    """
    pooldb = PoolDB(ctx.paths.pool_db())
    existing = locate.locate_worktree(ctx.paths, repo_name, branch)
    if existing is not None:
        acquired = _acquire_exact_sync_checkout(ctx, pooldb, repo_name, branch, existing)
        if acquired is not None:
            return acquired
        # The exact checkout belongs to another stacker operation. It must
        # release/detach before Git permits this invocation to check out the
        # branch elsewhere, so use the ordinary wait path below.
        return _acquire_ops_slot(ctx, pooldb, repo_name, branch)

    current = locate.slot_for_cwd(ctx.paths)
    if current is not None and current.repo_name == repo_name:
        acquired = _borrow_current_for_sync(pooldb, current, branch)
        if acquired is not None:
            return acquired

    return _acquire_ops_slot(ctx, pooldb, repo_name, branch)


def _acquire_exact_sync_checkout(
    ctx: StackerCtx,
    pooldb: PoolDB,
    repo_name: str,
    branch: str,
    path: Path,
) -> AcquiredSlot | None:
    current = locate.slot_for_cwd(ctx.paths, path)
    if (
        current is None
        or current.repo_name != repo_name
        or current.path.resolve() != path.resolve()
    ):
        raise git.GitError(
            f"Refusing to sync {selectors.selector_for(repo_name, branch)} in {path}: "
            "the branch is checked out outside the PM pool."
        )
    owner = pooldb.get_owner(repo_name, current.uuid)
    if owner is not None and owner.kind == OwnerKind.STACKER:
        return None
    _ensure_safe_sync_checkout(path, branch)
    if owner is not None:
        return AcquiredSlot(path=path, ops=None)

    candidate = slot_mod.Slot(repo=repo_name, uuid=current.uuid, path=current.path)
    try:
        pooldb.claim(repo_name, current.uuid, OWNER_STACKER_OPS)
    except slot_mod.SlotBusyError:
        # Ownership changed after discovery. Re-read it once: a project claim
        # is stable enough to reuse, while a stacker claim must be waited on.
        owner = pooldb.get_owner(repo_name, current.uuid)
        if owner is None or owner.kind == OwnerKind.STACKER:
            return None
        _ensure_safe_sync_checkout(path, branch)
        return AcquiredSlot(path=path, ops=None)
    try:
        _ensure_expected_branch(path, repo_name, branch)
        _ensure_safe_sync_checkout(path, branch)
    except BaseException:
        pooldb.release(repo_name, current.uuid)
        raise
    return AcquiredSlot(path=path, ops=candidate)


def _borrow_current_for_sync(
    pooldb: PoolDB,
    current: locate.CurrentSlot,
    branch: str,
) -> AcquiredSlot | None:
    owner = pooldb.get_owner(current.repo_name, current.uuid)
    if owner is not None and owner.kind == OwnerKind.STACKER:
        return None
    if _sync_checkout_blocker(current.path) is not None:
        return None

    candidate: slot_mod.Slot | None = None
    if owner is None:
        candidate = slot_mod.Slot(
            repo=current.repo_name,
            uuid=current.uuid,
            path=current.path,
        )
        try:
            pooldb.claim(current.repo_name, current.uuid, OWNER_STACKER_OPS)
        except slot_mod.SlotBusyError:
            return None
    try:
        # Revalidate after the atomic claim (or immediately before mutating a
        # project checkout) so a Git operation started during discovery is
        # never overwritten by checkout/reset.
        blocker = _sync_checkout_blocker(current.path)
        if blocker is not None:
            if candidate is not None:
                pooldb.release(current.repo_name, current.uuid)
            return None
        git.checkout(current.path, branch)
    except BaseException:
        if candidate is not None:
            pooldb.release(current.repo_name, current.uuid)
        raise
    return AcquiredSlot(path=current.path, ops=candidate)


def _ensure_safe_sync_checkout(path: Path, branch: str) -> None:
    blocker = _sync_checkout_blocker(path)
    if blocker is not None:
        raise git.GitError(
            f"Refusing to sync branch {branch!r} in dirty checkout {path}: {blocker}."
        )


def _ensure_expected_branch(path: Path, repo_name: str, branch: str) -> None:
    if git.current_branch(path) != branch:
        raise git.GitError(
            f"Checkout for {selectors.selector_for(repo_name, branch)} moved during "
            "sync acquisition; retry the command."
        )


def _sync_checkout_blocker(path: Path) -> str | None:
    in_progress = git.in_progress_operation(path)
    if in_progress is not None:
        return f"{in_progress} is in progress"
    if git.has_tracked_changes(path):
        return "Tracked changes are present"
    if git.has_untracked_files(path):
        return "Untracked files are present"
    return None


def _acquire_ops_slot(
    ctx: StackerCtx,
    pooldb: PoolDB,
    repo_name: str,
    branch: str,
) -> AcquiredSlot:
    claimed = ops_slot.acquire(
        ctx.paths,
        pooldb,
        repo_name,
        branch,
        wait=ops_slot.WaitOptions(progress=ctx.progress),
    )
    return AcquiredSlot(path=claimed.path, ops=claimed)


def release_if_owned(ctx: StackerCtx, acquired: AcquiredSlot) -> None:
    slot.release_if_owned(ctx, acquired)


def release_if_clean(ctx: StackerCtx, acquired: AcquiredSlot) -> None:
    slot.release_if_clean(ctx, acquired)


@contextmanager
def acquired_for_op(
    ctx: StackerCtx,
    repo_name: str,
    branch: str,
) -> Iterator[AcquiredSlot]:
    """Re-export of `slot.acquired_for_op` for the `worktree.` namespace."""
    with slot.acquired_for_op(ctx, repo_name, branch) as acquired:
        yield acquired


@contextmanager
def acquired_for_sync(
    ctx: StackerCtx,
    repo_name: str,
    branch: str,
) -> Iterator[AcquiredSlot]:
    acquired = acquire_for_sync(ctx, repo_name, branch)
    try:
        yield acquired
    finally:
        release_if_clean(ctx, acquired)


def slot_path_for_active_op(
    ctx: StackerCtx, op: OperationState, *, required: bool = True
) -> Path | None:
    if op.branch is None:
        return None
    path = locate.locate_worktree(ctx.paths, op.repo_name, op.branch)
    if path is None:
        if required:
            raise git.GitError(
                f"Operation references {selectors.selector_for(op.repo_name, op.branch)} "
                "but the branch is not checked out anywhere."
            )
        return None
    return path


def existing_ops_slot(
    ctx: StackerCtx, op: OperationState, slot_path: Path | None
) -> slot_mod.Slot | None:
    """Return the stacker-ops Slot handle backing this op's current branch, or None.

    A slot is an ops slot if the pool db records it as stacker-owned. A
    project-owned slot (or otherwise-claimed slot) returns None — we don't own it.
    """
    if slot_path is None:
        return None
    expected_pool = ctx.paths.pool(op.repo_name).resolve()
    if slot_path.parent.resolve() != expected_pool:
        return None
    pooldb = PoolDB(ctx.paths.pool_db())
    if pooldb.get_owner(op.repo_name, slot_path.name) != OWNER_STACKER_OPS:
        return None
    return slot_mod.Slot(repo=op.repo_name, uuid=slot_path.name, path=slot_path)


def require_checked_out(ctx: StackerCtx, repo_name: str, branch: str) -> Path:
    path = locate.locate_worktree(ctx.paths, repo_name, branch)
    if path is None:
        raise git.GitError(
            f"{selectors.selector_for(repo_name, branch)} is not checked out in any "
            "worktree. Check it out in a pm pool slot first."
        )
    return path


def repo_name_for_path(ctx: StackerCtx, worktree_path: Path) -> str | None:
    resolved = worktree_path.resolve()
    try:
        relative = resolved.relative_to(ctx.paths.worktrees.resolve())
    except ValueError:
        return None
    parts = relative.parts
    if not parts:
        return None
    return parts[0]


def run_single_pp(
    ctx: StackerCtx,
    tracked: TrackedBranch,
    logs: list[str],
    *,
    require_upstream_before: bool = False,
) -> bool:
    label = selectors.selector_for(tracked.repo_name, tracked.branch)
    path = require_checked_out(ctx, tracked.repo_name, tracked.branch)
    upstream = git.upstream_branch(path)
    if require_upstream_before and not upstream:
        fmt.record(ctx, logs, f"Stopping at {label}: no upstream remote is configured.")
        return False
    fmt.record(ctx, logs, f"Running git pp --force in {label}")
    # pp_force streams its output directly to the parent's stdout/stderr,
    # so the user sees push progress live instead of waiting silently.
    proc = git.pp_force(path)
    if proc.returncode != 0:
        raise git.GitError(f"git pp --force failed in {label}.")
    upstream_after = git.upstream_branch(path)
    if not upstream_after:
        fmt.record(ctx, logs, f"Stopping at {label}: no upstream remote is configured.")
        raise git.GitError(f"{label} has no upstream remote after git pp --force.")
    return True
