from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OWNER_STACKER_OPS, PoolDB
from project_manager.stacker import git as stacker_git
from project_manager.stacker import ops_slot

from .conftest import claim_forward


def _pooldb(paths: Paths) -> PoolDB:
    return PoolDB(paths.pool_db())


def _assert_owned_by_ops(slot: slot_mod.Slot, paths: Paths) -> None:
    assert _pooldb(paths).get_owner(slot.repo, slot.uuid) == OWNER_STACKER_OPS


@pytest.mark.usefixtures("stacker_repo")
def test_claim_records_ops_ownership(
    pm_env: Paths,
    three_slots: list[slot_mod.Slot],
) -> None:
    claimed = ops_slot.claim(pm_env, _pooldb(pm_env), "demo")
    _assert_owned_by_ops(claimed, pm_env)
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

    pooldb = _pooldb(pm_env)
    acquired = ops_slot.acquire(pm_env, pooldb, repo_name, "feature-a")
    head_ref = stacker_git.git(acquired.path, "symbolic-ref", "HEAD")
    assert head_ref.stdout.strip() == "refs/heads/feature-a"

    ops_slot.release(pooldb, acquired)
    assert pooldb.get_owner(acquired.repo, acquired.uuid) is None
    head_result = stacker_git.git(acquired.path, "symbolic-ref", "-q", "HEAD", check=False)
    assert head_result.returncode != 0  # detached


def test_crash_after_checkout_leaves_slot_claimed(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    repo_name, _ = stacker_repo
    stacker_git.git(three_slots[0].path, "checkout", "-b", "feature-crash")
    stacker_git.git(three_slots[0].path, "checkout", "--detach", "HEAD")

    acquired = ops_slot.acquire(pm_env, _pooldb(pm_env), repo_name, "feature-crash")
    # Simulate crash: exit without releasing.
    _assert_owned_by_ops(acquired, pm_env)
    branch = stacker_git.git(acquired.path, "symbolic-ref", "HEAD").stdout.strip()
    assert branch == "refs/heads/feature-crash"


def test_acquire_releases_claim_when_checkout_interrupted(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],  # noqa: ARG001 — populates the pool
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a Ctrl+C during `git checkout` (or any non-`GitError`
    raise inside the post-claim block) must still release the pool claim.

    The original `except GitError` clause caught the typed checkout
    failure but not `KeyboardInterrupt`; canceling a `pm stacker push`
    mid-acquire left the slot stuck as `stacker|ops` with no resumable
    state to recover, blocking subsequent pushes on pool exhaustion.
    """
    repo_name, _ = stacker_repo
    pooldb = _pooldb(pm_env)
    before_free = {s.uuid for s in slot_mod.free_slots(pm_env, pooldb, repo_name)}

    real_git = stacker_git.git

    def _interrupt_checkout(
        path: Path,
        *args: str,
        check: bool = True,
    ) -> object:
        if args[:1] == ("checkout",):
            raise KeyboardInterrupt
        return real_git(path, *args, check=check)

    monkeypatch.setattr(stacker_git, "git", _interrupt_checkout)

    with pytest.raises(KeyboardInterrupt):
        ops_slot.acquire(pm_env, pooldb, repo_name, "feature-irrelevant")

    after_free = {s.uuid for s in slot_mod.free_slots(pm_env, pooldb, repo_name)}
    assert after_free == before_free, (
        "interrupted acquire must not leak a pool claim: every slot tried got released"
    )


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

    pooldb = _pooldb(pm_env)
    before_free = {s.uuid for s in slot_mod.free_slots(pm_env, pooldb, repo_name)}
    with pytest.raises(stacker_git.GitError):
        ops_slot.acquire(pm_env, pooldb, repo_name, "feature-b")
    after_free = {s.uuid for s in slot_mod.free_slots(pm_env, pooldb, repo_name)}
    # Every candidate slot tried got released on failure.
    assert after_free == before_free


def test_pool_exhausted(pm_env: Paths, stacker_repo: tuple[str, Path]) -> None:
    repo_name, _ = stacker_repo
    # No pool slots exist (three_slots fixture not requested).
    with pytest.raises(slot_mod.PoolExhaustedError):
        ops_slot.claim(pm_env, _pooldb(pm_env), repo_name)


def test_claim_fails_fast_when_pool_full_and_no_stacker_owned(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    repo_name, _ = stacker_repo
    # All three slots are claimed by projects — stacker owns nothing.
    for i, s in enumerate(three_slots):
        claim_forward(pm_env, f"proj-{i}", repo_name, s)

    start = time.monotonic()
    with pytest.raises(slot_mod.PoolExhaustedError, match="no stacker-owned slot"):
        ops_slot.claim(
            pm_env,
            _pooldb(pm_env),
            repo_name,
            wait=ops_slot.WaitOptions(timeout_s=5.0, poll_interval_s=0.05),
        )
    # Did not wait — failure is immediate.
    assert time.monotonic() - start < 0.5


def test_claim_waits_for_stacker_slot_to_release(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    repo_name, _ = stacker_repo
    pooldb = _pooldb(pm_env)
    # Two slots claimed by projects, one by stacker.
    claim_forward(pm_env, "proj-0", repo_name, three_slots[0])
    claim_forward(pm_env, "proj-1", repo_name, three_slots[1])
    held = three_slots[2]
    pooldb.claim(repo_name, held.uuid, OWNER_STACKER_OPS)

    notices: list[str] = []

    def _release_later() -> None:
        time.sleep(0.15)
        pooldb.release(held.repo, held.uuid)

    threading.Thread(target=_release_later, daemon=True).start()
    claimed = ops_slot.claim(
        pm_env,
        pooldb,
        repo_name,
        wait=ops_slot.WaitOptions(timeout_s=5.0, poll_interval_s=0.05, progress=notices.append),
    )
    assert claimed.uuid == held.uuid
    assert any("Waiting" in n for n in notices)


def test_claim_times_out_when_stacker_slot_never_releases(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    repo_name, _ = stacker_repo
    pooldb = _pooldb(pm_env)
    claim_forward(pm_env, "proj-0", repo_name, three_slots[0])
    claim_forward(pm_env, "proj-1", repo_name, three_slots[1])
    pooldb.claim(repo_name, three_slots[2].uuid, OWNER_STACKER_OPS)

    with pytest.raises(slot_mod.PoolExhaustedError, match="stayed full"):
        ops_slot.claim(
            pm_env,
            pooldb,
            repo_name,
            wait=ops_slot.WaitOptions(timeout_s=0.2, poll_interval_s=0.05),
        )


def test_two_ops_on_different_branches_hold_distinct_slots(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    repo_name, _ = stacker_repo
    for name in ("feature-x", "feature-y"):
        stacker_git.git(three_slots[0].path, "checkout", "-b", name)
        stacker_git.git(three_slots[0].path, "checkout", "--detach", "HEAD")

    pooldb = _pooldb(pm_env)
    acquired_x = ops_slot.acquire(pm_env, pooldb, repo_name, "feature-x")
    acquired_y = ops_slot.acquire(pm_env, pooldb, repo_name, "feature-y")

    assert acquired_x.uuid != acquired_y.uuid
    for acq in (acquired_x, acquired_y):
        _assert_owned_by_ops(acq, pm_env)
