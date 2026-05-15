from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.stacker import git as stacker_git
from project_manager.stacker import locate


def test_locate_returns_slot_for_checked_out_branch(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    repo_name, _ = stacker_repo
    target = three_slots[0]
    stacker_git.git(target.path, "checkout", "-b", "feature-a")

    found = locate.locate_worktree(pm_env, repo_name, "feature-a")
    assert found is not None
    assert found.resolve() == target.path.resolve()


@pytest.mark.usefixtures("three_slots")
def test_locate_returns_none_for_absent_branch(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
) -> None:
    repo_name, _ = stacker_repo
    assert locate.locate_worktree(pm_env, repo_name, "nonexistent") is None


def test_locate_returns_none_for_unknown_repo(pm_env: Paths) -> None:
    assert locate.locate_worktree(pm_env, "nope", "main") is None


@pytest.mark.usefixtures("stacker_repo")
def test_slot_for_cwd_matches_slot_root(
    pm_env: Paths,
    three_slots: list[slot_mod.Slot],
) -> None:
    slot = three_slots[0]
    found = locate.slot_for_cwd(pm_env, slot.path)
    assert found is not None
    assert found.repo_name == slot.repo
    assert found.uuid == slot.uuid
    assert found.path.resolve() == slot.path.resolve()


@pytest.mark.usefixtures("stacker_repo")
def test_slot_for_cwd_matches_nested_path(
    pm_env: Paths,
    three_slots: list[slot_mod.Slot],
) -> None:
    slot = three_slots[0]
    nested = slot.path / "src" / "something"
    nested.mkdir(parents=True)
    found = locate.slot_for_cwd(pm_env, nested)
    assert found is not None
    assert found.uuid == slot.uuid


def test_slot_for_cwd_returns_none_outside_pool(pm_env: Paths, tmp_path: Path) -> None:
    assert locate.slot_for_cwd(pm_env, tmp_path) is None
    assert locate.slot_for_cwd(pm_env, pm_env.worktrees) is None
