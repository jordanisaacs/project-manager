from __future__ import annotations

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool import worktree as wt


def acquire(paths: Paths, repo_name: str, branch: str) -> slot_mod.Slot:
    """Claim a free pool slot for an ephemeral stacker op on `branch`.

    Steady-state release happens via `release` on clean completion. On crash
    mid-op the slot stays claimed so `stacker continue` / `abort` can drive
    resolution — do NOT wrap this in try/finally.
    """
    _ensure_marker(paths)
    free = slot_mod.free_slots(paths, repo_name)
    if not free:
        raise slot_mod.PoolExhaustedError(
            f"no free slot in pool {repo_name}; "
            f"run `pm pool add {repo_name}` or detach/delete a project"
        )
    target = free[0]
    slot_mod.claim(target, paths.stacker_ops_marker())
    try:
        wt._git(target.path, "checkout", branch)
    except wt.GitError:
        slot_mod.release(target)
        raise
    return target


def release(slot: slot_mod.Slot) -> None:
    """Return a stacker-ops slot to the pool with detached HEAD."""
    wt._git(slot.path, "checkout", "--detach", "HEAD")
    slot_mod.release(slot)


def _ensure_marker(paths: Paths) -> None:
    paths.stacker_root.mkdir(parents=True, exist_ok=True)
    marker = paths.stacker_ops_marker()
    if not marker.exists():
        marker.touch()
