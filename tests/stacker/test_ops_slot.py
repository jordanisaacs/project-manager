from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.stacker import git as stacker_git
from project_manager.stacker import ops_slot

from .conftest import claim_forward


def _assert_points_at_marker(slot: slot_mod.Slot, paths: Paths) -> None:
    target = slot.owner_target()
    assert target is not None
    assert target.resolve() == paths.stacker_ops_marker().resolve()


def test_claim_creates_marker_and_owner(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],  # noqa: ARG001
    three_slots: list[slot_mod.Slot],
) -> None:
    claimed = ops_slot.claim(pm_env, "demo")
    assert pm_env.stacker_ops_marker().is_file()
    assert claimed.owner_path.is_symlink()
    _assert_points_at_marker(claimed, pm_env)
    assert claimed.uuid in {s.uuid for s in three_slots}


def test_acquire_checks_out_branch_and_release_detaches(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    repo_name, _ = stacker_repo
    staging = three_slots[0]
    stacker_git.git(staging.path, "checkout", "-b", "feature-a")
    stacker_git.git(staging.path, "checkout", "--detach", "HEAD")

    acquired = ops_slot.acquire(pm_env, repo_name, "feature-a")
    head_ref = stacker_git.git(acquired.path, "symbolic-ref", "HEAD")
    assert head_ref.stdout.strip() == "refs/heads/feature-a"

    ops_slot.release(acquired)
    assert not acquired.owner_path.exists()
    head_result = stacker_git.git(
        acquired.path, "symbolic-ref", "-q", "HEAD", check=False
    )
    assert head_result.returncode != 0  # detached


def test_crash_after_checkout_leaves_slot_claimed(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    repo_name, _ = stacker_repo
    stacker_git.git(three_slots[0].path, "checkout", "-b", "feature-crash")
    stacker_git.git(three_slots[0].path, "checkout", "--detach", "HEAD")

    acquired = ops_slot.acquire(pm_env, repo_name, "feature-crash")
    # Simulate crash: exit without releasing.
    assert acquired.owner_path.is_symlink()
    _assert_points_at_marker(acquired, pm_env)
    branch = stacker_git.git(acquired.path, "symbolic-ref", "HEAD").stdout.strip()
    assert branch == "refs/heads/feature-crash"


def test_acquire_of_branch_held_elsewhere_releases_claim(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    repo_name, _ = stacker_repo
    # Check out feature-b in one slot and stake a project claim on it so
    # ops_slot.acquire picks a different slot.
    holder = three_slots[0]
    stacker_git.git(holder.path, "checkout", "-b", "feature-b")
    claim_forward(pm_env, "fake-project", repo_name, holder)

    before_free = {s.uuid for s in slot_mod.free_slots(pm_env, repo_name)}
    with pytest.raises(stacker_git.GitError):
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
        stacker_git.git(three_slots[0].path, "checkout", "-b", name)
        stacker_git.git(three_slots[0].path, "checkout", "--detach", "HEAD")

    acquired_x = ops_slot.acquire(pm_env, repo_name, "feature-x")
    acquired_y = ops_slot.acquire(pm_env, repo_name, "feature-y")

    assert acquired_x.uuid != acquired_y.uuid
    for acq in (acquired_x, acquired_y):
        _assert_points_at_marker(acq, pm_env)
