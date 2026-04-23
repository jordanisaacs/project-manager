import multiprocessing as mp

import pytest

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import Owner, OwnerKind, PoolDB
from project_manager.pool.slot import Slot, SlotBusyError


def _mk_pool(paths: Paths, repo: str, uuids: list[str]) -> list[Slot]:
    (paths.worktrees / repo).mkdir()
    for u in uuids:
        (paths.worktrees / repo / u).mkdir()
    return slot_mod.list_slots(paths, repo)


def _pooldb(paths: Paths) -> PoolDB:
    return PoolDB(paths.pool_db())


def test_list_empty(pm_env: Paths) -> None:
    assert slot_mod.list_slots(pm_env, "foo") == []


def test_list_and_free(pm_env: Paths) -> None:
    pooldb = _pooldb(pm_env)
    slots = _mk_pool(pm_env, "foo", ["a", "b"])
    assert [s.uuid for s in slots] == ["a", "b"]
    assert all(pooldb.is_free("foo", s.uuid) for s in slots)
    assert slot_mod.free_slots(pm_env, pooldb, "foo") == slots


def test_claim_release_roundtrip(pm_env: Paths) -> None:
    pooldb = _pooldb(pm_env)
    slots = _mk_pool(pm_env, "foo", ["a"])
    s = slots[0]
    owner = Owner(OwnerKind.PROJECT, "demo")
    pooldb.claim(s.repo, s.uuid, owner)
    assert not pooldb.is_free(s.repo, s.uuid)
    assert pooldb.get_owner(s.repo, s.uuid) == owner
    pooldb.release(s.repo, s.uuid)
    assert pooldb.is_free(s.repo, s.uuid)


def test_double_claim_raises(pm_env: Paths) -> None:
    pooldb = _pooldb(pm_env)
    slots = _mk_pool(pm_env, "foo", ["a"])
    s = slots[0]
    pooldb.claim(s.repo, s.uuid, Owner(OwnerKind.PROJECT, "demo"))
    with pytest.raises(SlotBusyError):
        pooldb.claim(s.repo, s.uuid, Owner(OwnerKind.PROJECT, "other"))


def test_release_idempotent(pm_env: Paths) -> None:
    pooldb = _pooldb(pm_env)
    slots = _mk_pool(pm_env, "foo", ["a"])
    pooldb.release(slots[0].repo, slots[0].uuid)
    pooldb.release(slots[0].repo, slots[0].uuid)


def _claim_child(
    paths: Paths, repo: str, uuid: str, index: int, queue: "mp.Queue[str]",
) -> None:
    """Run in a spawned child; report outcome to `queue`."""
    pooldb = PoolDB(paths.pool_db())
    try:
        pooldb.claim(repo, uuid, Owner(OwnerKind.PROJECT, f"p{index}"))
    except SlotBusyError:
        queue.put("L")
    except Exception as e:  # noqa: BLE001 — child reports any failure via queue
        queue.put(f"E{e}")
    else:
        queue.put(f"W{index}")


def test_concurrent_claim_exactly_one_wins(pm_env: Paths) -> None:
    slots = _mk_pool(pm_env, "foo", ["a"])
    s = slots[0]
    # Prime the schema from the parent before spawning to avoid a "CREATE TABLE"
    # race producing duplicate IntegrityErrors across children.
    _pooldb(pm_env).is_free(s.repo, s.uuid)

    # `spawn` starts a fresh Python in each child — no fork-from-threaded-process
    # hazard (pytest-xdist workers are multi-threaded).
    ctx = mp.get_context("spawn")
    queue: mp.Queue[str] = ctx.Queue()
    n = 8
    procs = [
        ctx.Process(target=_claim_child, args=(pm_env, s.repo, s.uuid, i, queue))
        for i in range(n)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join()

    results = [queue.get() for _ in range(n)]
    wins = [r for r in results if r.startswith("W")]
    losses = [r for r in results if r == "L"]
    errors = [r for r in results if not (r.startswith("W") or r == "L")]

    assert errors == [], f"unexpected errors: {errors}"
    assert len(wins) == 1, f"expected 1 winner, got {len(wins)} (results={results})"
    assert len(losses) == n - 1

    winner_idx = int(wins[0][1:])
    assert _pooldb(pm_env).get_owner(s.repo, s.uuid) == Owner(
        OwnerKind.PROJECT, f"p{winner_idx}"
    )
