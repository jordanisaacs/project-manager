import os

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


def test_concurrent_claim_exactly_one_wins(pm_env: Paths) -> None:
    slots = _mk_pool(pm_env, "foo", ["a"])
    s = slots[0]
    # Prime the schema from the parent before fork to avoid a "CREATE TABLE"
    # race producing duplicate IntegrityErrors across forked children.
    _pooldb(pm_env).is_free(s.repo, s.uuid)

    n = 8
    pipes = [os.pipe() for _ in range(n)]
    pids: list[int] = []
    for i in range(n):
        pid = os.fork()
        if pid == 0:
            for j, (r, w) in enumerate(pipes):
                if j != i:
                    os.close(r)
                    os.close(w)
            r, w = pipes[i]
            os.close(r)
            try:
                pooldb = _pooldb(pm_env)
                pooldb.claim(s.repo, s.uuid, Owner(OwnerKind.PROJECT, f"p{i}"))
                os.write(w, f"W{i}".encode())
            except SlotBusyError:
                os.write(w, b"L")
            except Exception as e:  # noqa: BLE001 — child reports any failure via pipe
                os.write(w, f"E{e}".encode())
            os.close(w)
            os._exit(0)
        pids.append(pid)

    for pid in pids:
        os.waitpid(pid, 0)

    results = []
    for r, w in pipes:
        os.close(w)
        results.append(os.read(r, 128))
        os.close(r)

    wins = [r for r in results if r.startswith(b"W")]
    losses = [r for r in results if r == b"L"]
    errors = [r for r in results if not (r.startswith(b"W") or r == b"L")]

    assert errors == [], f"unexpected errors: {errors}"
    assert len(wins) == 1, f"expected 1 winner, got {len(wins)} (results={results})"
    assert len(losses) == n - 1

    winner_idx = int(wins[0][1:].decode())
    assert _pooldb(pm_env).get_owner(s.repo, s.uuid) == Owner(
        OwnerKind.PROJECT, f"p{winner_idx}"
    )
