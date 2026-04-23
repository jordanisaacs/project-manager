from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OWNER_STACKER_OPS, PoolDB
from project_manager.stacker import git as stacker_git
from project_manager.stacker.commands import _common
from project_manager.stacker.commands.create import _resolve_create_slot


@pytest.fixture
def pooldb(pm_env: Paths) -> PoolDB:
    return PoolDB(pm_env.pool_db())


def test_resolve_uses_current_slot_when_cwd_is_pm_slot(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    pooldb: PoolDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, _ = stacker_repo
    here = three_slots[0]
    monkeypatch.chdir(here.path)

    worktree, cleanup = _resolve_create_slot(pm_env, pooldb, repo_name)
    assert worktree.resolve() == here.path.resolve()
    # Cleanup on the current-slot branch is a no-op — we never claimed.
    cleanup()
    # Slot ownership unchanged (still FREE).
    assert pooldb.get_owner(here.repo, here.uuid) is None


def test_resolve_claims_fresh_slot_when_cwd_outside_pool(  # noqa: PLR0913 (fixture plumbing)
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    pooldb: PoolDB,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_name, _ = stacker_repo
    monkeypatch.chdir(tmp_path)

    worktree, cleanup = _resolve_create_slot(pm_env, pooldb, repo_name)
    slot_uuids = {s.uuid for s in three_slots}
    assert worktree.parent.resolve() == pm_env.pool(repo_name).resolve()
    assert worktree.name in slot_uuids
    # The claimed slot is owned by stacker ops.
    assert pooldb.get_owner(repo_name, worktree.name) == OWNER_STACKER_OPS
    # Cleanup releases it.
    cleanup()
    assert pooldb.get_owner(repo_name, worktree.name) is None


def test_resolve_repo_uses_cwd_slot_when_args_empty(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, _ = stacker_repo
    monkeypatch.chdir(three_slots[0].path)
    args = argparse.Namespace(repo=None)
    assert _common.resolve_repo(args, pm_env) == repo_name


def test_resolve_repo_prefers_explicit_arg(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],  # noqa: ARG001 (creates repo)
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(three_slots[0].path)
    args = argparse.Namespace(repo="explicit")
    assert _common.resolve_repo(args, pm_env) == "explicit"


def test_resolve_repo_errors_when_outside_slot_and_no_arg(
    pm_env: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    args = argparse.Namespace(repo=None)
    with pytest.raises(stacker_git.GitError, match="pass --repo"):
        _common.resolve_repo(args, pm_env)


def test_resolve_branch_uses_cwd_current_branch(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],  # noqa: ARG001
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot = three_slots[0]
    stacker_git.git(slot.path, "checkout", "-b", "feature-cwd")
    monkeypatch.chdir(slot.path)
    args = argparse.Namespace(branch=None)
    assert _common.resolve_branch(args, pm_env) == "feature-cwd"


def test_resolve_branch_errors_on_detached_head(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],  # noqa: ARG001
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot = three_slots[0]
    monkeypatch.chdir(slot.path)  # three_slots creates them detached
    args = argparse.Namespace(branch=None)
    with pytest.raises(stacker_git.GitError, match="detached"):
        _common.resolve_branch(args, pm_env)


def test_resolve_claims_fresh_slot_when_cwd_is_in_different_repo(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    pooldb: PoolDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # cwd is in `demo`'s pool but we ask to create in a different repo.
    repo_name, _ = stacker_repo
    monkeypatch.chdir(three_slots[0].path)

    # Make a second pool dir so the claim has somewhere to go.
    other_pool = pm_env.pool("other")
    other_pool.mkdir()
    # We don't actually need a slot to exist; the function should raise
    # PoolExhaustedError when the other pool has no FREE slots and no
    # stacker-owned slots.
    with pytest.raises(slot_mod.PoolExhaustedError):
        _resolve_create_slot(pm_env, pooldb, "other")
    assert repo_name != "other"
