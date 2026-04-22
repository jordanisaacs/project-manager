import os
from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.slot import Slot, SlotBusyError


def _mk_pool(paths: Paths, repo: str, uuids: list[str]) -> list[Slot]:
    (paths.worktrees / repo).mkdir()
    for u in uuids:
        (paths.worktrees / repo / u).mkdir()
    return slot_mod.list_slots(paths, repo)


def test_list_empty(pm_env: Paths) -> None:
    assert slot_mod.list_slots(pm_env, "foo") == []


def test_list_and_free(pm_env: Paths) -> None:
    slots = _mk_pool(pm_env, "foo", ["a", "b"])
    assert [s.uuid for s in slots] == ["a", "b"]
    assert all(s.is_free() for s in slots)
    assert slot_mod.free_slots(pm_env, "foo") == slots


def test_claim_release_roundtrip(pm_env: Paths) -> None:
    slots = _mk_pool(pm_env, "foo", ["a"])
    s = slots[0]
    forward = pm_env.forward("demo", "foo")
    slot_mod.claim(s, forward)
    assert not s.is_free()
    assert s.owner_target() == forward
    slot_mod.release(s)
    assert s.is_free()


def test_double_claim_raises(pm_env: Paths) -> None:
    slots = _mk_pool(pm_env, "foo", ["a"])
    s = slots[0]
    slot_mod.claim(s, pm_env.forward("demo", "foo"))
    with pytest.raises(SlotBusyError):
        slot_mod.claim(s, pm_env.forward("other", "foo"))


def test_release_idempotent(pm_env: Paths) -> None:
    slots = _mk_pool(pm_env, "foo", ["a"])
    slot_mod.release(slots[0])
    slot_mod.release(slots[0])


def test_concurrent_claim_exactly_one_wins(pm_env: Paths) -> None:
    slots = _mk_pool(pm_env, "foo", ["a"])
    s = slots[0]

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
                slot_mod.claim(s, pm_env.forward(f"p{i}", "foo"))
                os.write(w, b"W")
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

    wins = sum(1 for r in results if r == b"W")
    losses = sum(1 for r in results if r == b"L")
    errors = [r for r in results if r not in (b"W", b"L")]

    assert errors == [], f"unexpected errors: {errors}"
    assert wins == 1, f"expected 1 winner, got {wins} (results={results})"
    assert losses == n - 1

    assert s.owner_target() is not None
    winner_idx = results.index(b"W")
    assert s.owner_target() == Path(str(pm_env.forward(f"p{winner_idx}", "foo")))
