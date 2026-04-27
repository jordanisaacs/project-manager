from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.stacker import git as stacker_git
from project_manager.stacker import locate
from project_manager.stacker.models import ParentLocator, SelectorTarget, WorktreeInit
from project_manager.stacker.service import StackerService

from .conftest import commit_file


def test_init_new_branch_off_parent_tracks(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    """Creates a new branch at parent's tip, tracks with managed_base = parent_tip."""
    repo_name, _ = stacker_repo
    slot = three_slots[0]
    tracked = service.init_new_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=slot.path,
            branch="feature-a",
            parent=ParentLocator(repo_name=repo_name, branch="main"),
        )
    )
    assert tracked.parent_branch == "main"
    main_head = stacker_git.rev_parse(pm_env.repo(repo_name), "main")
    assert tracked.managed_base_commit == main_head


@pytest.mark.usefixtures("pm_env")
def test_init_new_branch_with_copy_from_imports_commits(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    """`copy_from` seeds the branch with another branch's history.

    The new branch's tip is `copy_from`'s tip; its managed_base is
    still `parent`'s tip, so next sync sees the copied commits as
    cherry-pick candidates.
    """
    repo_name, repo_path = stacker_repo
    # Create a donor branch with a unique commit.
    donor_slot = three_slots[0]
    service.init_new_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=donor_slot.path,
            branch="donor",
            parent=ParentLocator(repo_name=repo_name, branch="main"),
        )
    )
    donor_head = commit_file(donor_slot.path, "donor.txt", "d\n", "donor: commit")

    # Create new branch off main but starting from donor's tip.
    new_slot = three_slots[1]
    tracked = service.init_new_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=new_slot.path,
            branch="copy-target",
            parent=ParentLocator(repo_name=repo_name, branch="main"),
            copy_from="donor",
        )
    )
    new_head = stacker_git.rev_parse(new_slot.path, "HEAD")
    main_head = stacker_git.rev_parse(repo_path, "main")
    assert new_head == donor_head
    assert tracked.managed_base_commit == main_head


def test_create_tracked_branch_without_worktree_slot(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    service: StackerService,
) -> None:
    """`--no-checkout` path: branch ref exists, no slot is claimed."""
    repo_name, repo_path = stacker_repo
    tracked = service.create_tracked_branch(
        repo_name, "ghost-branch",
        ParentLocator(repo_name=repo_name, branch="main"),
    )
    assert tracked.branch == "ghost-branch"
    assert stacker_git.branch_exists(repo_path, "ghost-branch")
    # No worktree reports the branch as checked out.
    assert locate.locate_worktree(pm_env, repo_name, "ghost-branch") is None


def test_create_tracked_branch_errors_when_branch_exists(
    stacker_repo: tuple[str, Path],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    stacker_git.git(repo_path, "branch", "existing", "main")
    with pytest.raises(stacker_git.GitError, match="already exists"):
        service.create_tracked_branch(
            repo_name, "existing",
            ParentLocator(repo_name=repo_name, branch="main"),
        )


def test_track_adopts_existing_branch(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    """The `create --replace` path calls `service.track()`."""
    repo_name, repo_path = stacker_repo
    stacker_git.git(repo_path, "branch", "feature-existing", "main")
    slot = three_slots[0]
    stacker_git.git(slot.path, "checkout", "feature-existing")

    tracked = service.track(
        SelectorTarget(repo_name=repo_name, branch="feature-existing"),
        ParentLocator(repo_name=repo_name, branch="main"),
    )
    assert tracked.parent_branch == "main"


def test_track_adopts_via_cwd_when_branch_unchecked_out(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cwd-detached + branch ref exists → track() checks it out in cwd, no fresh slot."""
    repo_name, repo_path = stacker_repo
    stacker_git.git(repo_path, "branch", "feature-detached", "main")
    here = three_slots[0]
    monkeypatch.chdir(here.path)

    tracked = service.track(
        SelectorTarget(repo_name=repo_name, branch="feature-detached"),
        ParentLocator(repo_name=repo_name, branch="main"),
    )
    assert tracked.parent_branch == "main"
    # Cwd slot is now on the adopted branch.
    assert stacker_git.current_branch(here.path) == "feature-detached"
    # No claim taken on the cwd slot.
    from project_manager.pool.db import PoolDB
    assert PoolDB(pm_env.pool_db()).get_owner(here.repo, here.uuid) is None


def test_track_adopts_via_ops_slot_when_cwd_unsuitable(  # noqa: PLR0913 (fixture plumbing)
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cwd outside pool + branch unchecked-out → track() claims a fresh ops slot."""
    from project_manager.pool.db import OWNER_STACKER_OPS, PoolDB

    repo_name, repo_path = stacker_repo
    stacker_git.git(repo_path, "branch", "feature-orphan", "main")
    monkeypatch.chdir(tmp_path)

    tracked = service.track(
        SelectorTarget(repo_name=repo_name, branch="feature-orphan"),
        ParentLocator(repo_name=repo_name, branch="main"),
    )
    assert tracked.parent_branch == "main"
    # An ops slot was claimed and the branch is now checked out there.
    pooldb = PoolDB(pm_env.pool_db())
    claimed = [
        s for s in three_slots
        if pooldb.get_owner(s.repo, s.uuid) == OWNER_STACKER_OPS
    ]
    assert len(claimed) == 1
    assert stacker_git.current_branch(claimed[0].path) == "feature-orphan"
