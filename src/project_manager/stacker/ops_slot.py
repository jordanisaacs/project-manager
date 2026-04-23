from __future__ import annotations

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OWNER_STACKER_OPS, PoolDB

from . import git


def claim(paths: Paths, pooldb: PoolDB, repo_name: str) -> slot_mod.Slot:
    """Claim a free pool slot as an ephemeral stacker ops slot.

    The slot is recorded in the pool db as owned by the stacker (owner_kind=
    'stacker', owner_id='ops') so it shows up as OPS to pm's check / ls.
    Does not check out any branch — callers do that themselves.
    """
    free = slot_mod.free_slots(paths, pooldb, repo_name)
    if not free:
        raise slot_mod.PoolExhaustedError(
            f"no free slot in pool {repo_name}; "
            f"run `pm pool add {repo_name}` or detach/delete a project"
        )
    target = free[0]
    pooldb.claim(repo_name, target.uuid, OWNER_STACKER_OPS)
    return target


def acquire(paths: Paths, pooldb: PoolDB, repo_name: str, branch: str) -> slot_mod.Slot:
    """Claim a free pool slot and check `branch` out in it.

    Steady-state release happens via `release` on clean completion. On crash
    mid-op the slot stays claimed so `stacker continue` / `abort` can drive
    resolution — do NOT wrap this in try/finally.
    """
    target = claim(paths, pooldb, repo_name)
    try:
        git.git(target.path, "checkout", branch)
    except git.GitError:
        pooldb.release(repo_name, target.uuid)
        raise
    return target


def release(pooldb: PoolDB, slot: slot_mod.Slot) -> None:
    """Return a stacker-ops slot to the pool with detached HEAD."""
    git.git(slot.path, "checkout", "--detach", "HEAD")
    pooldb.release(slot.repo, slot.uuid)
