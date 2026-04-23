from __future__ import annotations

from pathlib import Path

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool import worktree as wt
from project_manager.stacker import locate


def test_locate_returns_slot_for_checked_out_branch(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    repo_name, _ = stacker_repo
    target = three_slots[0]
    wt._git(target.path, "checkout", "-b", "feature-a")

    found = locate.locate_worktree(pm_env, repo_name, "feature-a")
    assert found is not None
    assert Path(found).resolve() == target.path.resolve()


def test_locate_returns_none_for_absent_branch(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],  # noqa: ARG001
) -> None:
    repo_name, _ = stacker_repo
    assert locate.locate_worktree(pm_env, repo_name, "nonexistent") is None


def test_locate_returns_none_for_unknown_repo(pm_env: Paths) -> None:
    assert locate.locate_worktree(pm_env, "nope", "main") is None
