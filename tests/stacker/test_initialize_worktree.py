from __future__ import annotations

from pathlib import Path

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool import worktree as wt
from project_manager.stacker import locate
from project_manager.stacker.models import ParentLocator
from project_manager.stacker.service import StackerService


def test_create_branch_off_parent_tracks(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, _ = stacker_repo
    target = three_slots[0]
    parent = ParentLocator(repo_name=repo_name, branch="main")

    tracked = service.initialize_worktree(
        repo_name=repo_name,
        worktree_path=target.path,
        branch="feature-a",
        create_branch=True,
        parent=parent,
    )
    assert tracked is not None
    assert tracked.branch == "feature-a"
    assert tracked.parent_branch == "main"
    # Branch is actually checked out.
    head_ref = wt._git(target.path, "symbolic-ref", "HEAD").stdout.strip()
    assert head_ref == "refs/heads/feature-a"
    assert locate.locate_worktree(pm_env, repo_name, "feature-a") is not None


def test_adopt_existing_branch_tracks(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    # Pre-create the branch in the bare repo.
    wt._git(repo_path, "branch", "feature-b", "main")
    target = three_slots[0]
    parent = ParentLocator(repo_name=repo_name, branch="main")

    tracked = service.initialize_worktree(
        repo_name=repo_name,
        worktree_path=target.path,
        branch="feature-b",
        create_branch=False,
        parent=parent,
    )
    assert tracked is not None
    assert tracked.branch == "feature-b"
    head_ref = wt._git(target.path, "symbolic-ref", "HEAD").stdout.strip()
    assert head_ref == "refs/heads/feature-b"


def test_plain_checkout_without_tracking(
    pm_env: Paths,  # noqa: ARG001
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    wt._git(repo_path, "branch", "feature-c", "main")
    target = three_slots[0]

    tracked = service.initialize_worktree(
        repo_name=repo_name,
        worktree_path=target.path,
        branch="feature-c",
        create_branch=False,
        parent=None,
    )
    assert tracked is None
    head_ref = wt._git(target.path, "symbolic-ref", "HEAD").stdout.strip()
    assert head_ref == "refs/heads/feature-c"
