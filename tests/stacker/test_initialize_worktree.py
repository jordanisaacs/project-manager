from __future__ import annotations

from pathlib import Path

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.stacker import git as stacker_git
from project_manager.stacker import locate
from project_manager.stacker.models import ParentLocator, WorktreeInit
from project_manager.stacker.service import StackerService


def _head_ref(path: Path) -> str:
    return stacker_git.git(path, "symbolic-ref", "HEAD").stdout.strip()


def test_create_branch_off_parent_tracks(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, _ = stacker_repo
    target = three_slots[0]

    tracked = service.init_new_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=target.path,
            branch="feature-a",
            parent=ParentLocator(repo_name=repo_name, branch="main"),
        )
    )
    assert tracked.branch == "feature-a"
    assert tracked.parent_branch == "main"
    assert _head_ref(target.path) == "refs/heads/feature-a"
    assert locate.locate_worktree(pm_env, repo_name, "feature-a") is not None


def test_adopt_existing_branch_tracks(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    stacker_git.git(repo_path, "branch", "feature-b", "main")
    target = three_slots[0]

    tracked = service.init_adopt_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=target.path,
            branch="feature-b",
            parent=ParentLocator(repo_name=repo_name, branch="main"),
        )
    )
    assert tracked is not None
    assert tracked.branch == "feature-b"
    assert _head_ref(target.path) == "refs/heads/feature-b"


def test_plain_checkout_without_tracking(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    stacker_git.git(repo_path, "branch", "feature-c", "main")
    target = three_slots[0]

    tracked = service.init_adopt_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=target.path,
            branch="feature-c",
            parent=None,
        )
    )
    assert tracked is None
    assert _head_ref(target.path) == "refs/heads/feature-c"
