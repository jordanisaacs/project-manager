from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OWNER_STACKER_OPS, OwnerKind, PoolDB

from . import git


@dataclass(frozen=True)
class WaitOptions:
    """Controls the poll-and-wait loop when the pool is full."""

    timeout_s: float = 120.0
    poll_interval_s: float = 0.5
    progress: Callable[[str], None] | None = None


def claim(
    paths: Paths,
    pooldb: PoolDB,
    repo_name: str,
    *,
    wait: WaitOptions | None = None,
) -> slot_mod.Slot:
    """Claim a pool slot for stacker work.

    - A free slot is claimed immediately.
    - If the pool is full but stacker owns ≥1 slot, block and poll until one
      releases (up to `wait_timeout_s`). Rationale: project-owned slots are
      long-lived; stacker-owned slots are bound to stacker ops which will
      eventually release.
    - If the pool is full and stacker owns zero slots, fail fast — nothing
      will free up on its own.

    The slot is recorded in the pool db as owned by the stacker (owner_kind=
    'stacker', owner_id='ops') so it shows up as OPS to pm's check / ls.
    Does not check out any branch — callers do that themselves.
    """
    opts = wait or WaitOptions()
    deadline = time.monotonic() + opts.timeout_s
    notified = False
    while True:
        claimed = _try_claim_free(paths, pooldb, repo_name)
        if claimed is not None:
            return claimed
        if not _stacker_owns_any(pooldb, repo_name):
            raise slot_mod.PoolExhaustedError(
                f"pool {repo_name} has no free slot and no stacker-owned slot to wait on; "
                f"run `pm pool add {repo_name}` before starting stacker work here."
            )
        if time.monotonic() >= deadline:
            raise slot_mod.PoolExhaustedError(
                f"pool {repo_name} stayed full for {opts.timeout_s:.0f}s "
                f"waiting for a stacker slot to release."
            )
        if not notified and opts.progress is not None:
            opts.progress(f"Waiting for a stacker slot in pool {repo_name} to release...")
            notified = True
        time.sleep(opts.poll_interval_s)


def _try_claim_free(
    paths: Paths, pooldb: PoolDB, repo_name: str
) -> slot_mod.Slot | None:
    """One claim attempt. Returns the slot on success; None if pool is full.

    Handles the race where another claimer grabs our target between the
    free-scan and the pool-db insert: retries within this call until either
    a claim lands or the pool is empty.
    """
    while True:
        free = slot_mod.free_slots(paths, pooldb, repo_name)
        if not free:
            return None
        target = free[0]
        try:
            pooldb.claim(repo_name, target.uuid, OWNER_STACKER_OPS)
        except slot_mod.SlotBusyError:
            continue
        return target


def _stacker_owns_any(pooldb: PoolDB, repo_name: str) -> bool:
    return any(
        owner.kind == OwnerKind.STACKER
        for (_repo, _uuid, owner) in pooldb.list_owned(repo_name)
    )


def acquire(
    paths: Paths,
    pooldb: PoolDB,
    repo_name: str,
    branch: str,
    *,
    wait: WaitOptions | None = None,
) -> slot_mod.Slot:
    """Claim a pool slot and check `branch` out in it.

    Steady-state release happens via `release` on clean completion. The
    pre-completion window — claim succeeded, checkout running — catches
    `BaseException` so a `KeyboardInterrupt` (or any unexpected error)
    releases the just-taken claim instead of orphaning it. There's no
    resumable op for a single-shot acquire and the worktree is clean by
    construction at this point, so the slot is safe to return to the
    pool. Once this returns, the caller's own `try/finally` (with
    `release_if_clean`) governs.
    """
    target = claim(paths, pooldb, repo_name, wait=wait)
    try:
        git.git(target.path, "checkout", branch)
    except BaseException:
        pooldb.release(repo_name, target.uuid)
        raise
    return target


def release(pooldb: PoolDB, slot: slot_mod.Slot) -> None:
    """Return a stacker-ops slot to the pool with detached HEAD."""
    git.detach_head(slot.path)
    pooldb.release(slot.repo, slot.uuid)
