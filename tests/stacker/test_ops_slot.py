from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool import worktree as wt
from project_manager.stacker import ops_slot


def test_claim_creates_marker_and_owner(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],  # noqa: ARG001
    three_slots: list[slot_mod.Slot],
) -> None:
    claimed = ops_slot.claim(pm_env, "demo")
    assert pm_env.stacker_ops_marker().is_file()
    assert claimed.owner_path.is_symlink()
    assert claimed.owner_target().resolve() == pm_env.stacker_ops_marker().resolve()
    # Original free slots include the claimed one.
    assert claimed.uuid in {s.uuid for s in three_slots}


def test_acquire_checks_out_branch_and_release_detaches(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    repo_name, _ = stacker_repo
    staging = three_slots[0]
    wt._git(staging.path, "checkout", "-b", "feature-a")
    wt._git(staging.path, "checkout", "--detach", "HEAD")

    acquired = ops_slot.acquire(pm_env, repo_name, "feature-a")
    head_ref = wt._git(acquired.path, "symbolic-ref", "HEAD")
    assert head_ref.stdout.strip() == "refs/heads/feature-a"

    ops_slot.release(acquired)
    assert not acquired.owner_path.exists()
    head_result = wt._git(acquired.path, "symbolic-ref", "-q", "HEAD", check=False)
    assert head_result.returncode != 0  # detached


def test_crash_after_checkout_leaves_slot_claimed(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    repo_name, _ = stacker_repo
    wt._git(three_slots[0].path, "checkout", "-b", "feature-crash")
    wt._git(three_slots[0].path, "checkout", "--detach", "HEAD")

    acquired = ops_slot.acquire(pm_env, repo_name, "feature-crash")
    # Simulate crash: exit without releasing.
    assert acquired.owner_path.is_symlink()
    assert acquired.owner_target().resolve() == pm_env.stacker_ops_marker().resolve()
    branch = wt._git(acquired.path, "symbolic-ref", "HEAD").stdout.strip()
    assert branch == "refs/heads/feature-crash"


def test_acquire_of_branch_held_elsewhere_releases_claim(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    from .conftest import claim_forward

    repo_name, _ = stacker_repo
    # Check out feature-b in one slot and stake a project claim on it so
    # ops_slot.acquire picks a different slot.
    holder = three_slots[0]
    wt._git(holder.path, "checkout", "-b", "feature-b")
    claim_forward(pm_env, "fake-project", repo_name, holder)

    before_free = {s.uuid for s in slot_mod.free_slots(pm_env, repo_name)}
    with pytest.raises(wt.GitError):
        ops_slot.acquire(pm_env, repo_name, "feature-b")
    after_free = {s.uuid for s in slot_mod.free_slots(pm_env, repo_name)}
    # Every candidate slot tried got released on failure.
    assert after_free == before_free


def test_pool_exhausted(pm_env: Paths, stacker_repo: tuple[str, Path]) -> None:
    repo_name, _ = stacker_repo
    # No pool slots exist (three_slots fixture not requested).
    with pytest.raises(slot_mod.PoolExhaustedError):
        ops_slot.claim(pm_env, repo_name)


def test_two_ops_on_different_branches_hold_distinct_slots(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    repo_name, _ = stacker_repo
    for name in ("feature-x", "feature-y"):
        wt._git(three_slots[0].path, "checkout", "-b", name)
        wt._git(three_slots[0].path, "checkout", "--detach", "HEAD")

    acquired_x = ops_slot.acquire(pm_env, repo_name, "feature-x")
    acquired_y = ops_slot.acquire(pm_env, repo_name, "feature-y")

    assert acquired_x.uuid != acquired_y.uuid
    for acq in (acquired_x, acquired_y):
        assert acq.owner_target().resolve() == pm_env.stacker_ops_marker().resolve()
