from __future__ import annotations

from pathlib import Path

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool import worktree as wt
from project_manager.stacker import ops_slot
from project_manager.stacker.cli import gc_ops
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import OperationState


def test_gc_ops_releases_orphan_slot(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    repo_name, _ = stacker_repo
    # Pre-create a branch and simulate an ops slot that was left claimed with
    # no matching operations row.
    wt._git(three_slots[0].path, "checkout", "-b", "orphaned")
    wt._git(three_slots[0].path, "checkout", "--detach", "HEAD")
    ops_slot.acquire(pm_env, repo_name, "orphaned")

    released = gc_ops(pm_env)
    assert len(released) == 1
    assert released[0].owner_target() is None


def test_gc_ops_leaves_live_op_alone(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    repo_name, _ = stacker_repo
    wt._git(three_slots[0].path, "checkout", "-b", "live")
    wt._git(three_slots[0].path, "checkout", "--detach", "HEAD")
    acquired = ops_slot.acquire(pm_env, repo_name, "live")

    # Simulate a live operation row matching the claimed branch.
    db = StackerDB(pm_env.stacker_db())
    db.put_operation(
        OperationState(
            repo_name=repo_name,
            op_type="local_sync",
            status="paused",
            branch="live",
        )
    )

    released = gc_ops(pm_env)
    assert released == []
    assert acquired.owner_target() is not None
