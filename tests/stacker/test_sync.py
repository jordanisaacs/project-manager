from __future__ import annotations

from pathlib import Path

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OWNER_STACKER_OPS, PoolDB
from project_manager.stacker import git as stacker_git
from project_manager.stacker import locate
from project_manager.stacker.models import ParentLocator, SelectorTarget
from project_manager.stacker.service import StackerService

from .conftest import commit_file


def _initialize(
    service: StackerService, repo_name: str, slot: slot_mod.Slot, branch: str
) -> None:
    service.initialize_worktree(
        repo_name=repo_name,
        worktree_path=slot.path,
        branch=branch,
        create_branch=True,
        parent=ParentLocator(repo_name=repo_name, branch="main"),
    )


def _advance_main(repo_path: Path) -> str:
    """Commit directly to main in the pm 'repo' (which has main checked out)."""
    return commit_file(repo_path, "extra.txt", "extra\n", "extra")


def test_sync_against_live_worktree(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    _initialize(service, repo_name, feature_slot, "feature-a")
    commit_file(feature_slot.path, "feat.txt", "work\n", "feature work")

    new_main = _advance_main(repo_path)
    assert new_main != ""

    result = service.sync(SelectorTarget(repo_name=repo_name, branch="feature-a"))
    assert "Sync complete." in result
    tracked = service.db.get_branch(repo_name, "feature-a")
    assert tracked is not None
    assert tracked.managed_base_commit == new_main


def test_sync_branch_not_checked_out_acquires_ops_slot(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    _initialize(service, repo_name, feature_slot, "feature-b")
    commit_file(feature_slot.path, "feat.txt", "work\n", "feature work")
    # Simulate the branch moving out of any pool slot: detach the slot.
    stacker_git.git(feature_slot.path, "checkout", "--detach", "HEAD")
    pooldb = PoolDB(pm_env.pool_db())
    if not pooldb.is_free(feature_slot.repo, feature_slot.uuid):
        pooldb.release(feature_slot.repo, feature_slot.uuid)
    assert locate.locate_worktree(pm_env, repo_name, "feature-b") is None

    _advance_main(repo_path)

    result = service.sync(SelectorTarget(repo_name=repo_name, branch="feature-b"))
    assert "Sync complete." in result
    # After clean completion no slot should be held by the ops marker.
    for slot in slot_mod.list_slots(pm_env, repo_name):
        owner = pooldb.get_owner(slot.repo, slot.uuid)
        assert owner != OWNER_STACKER_OPS


def test_sync_paused_on_conflict_keeps_slot_claimed(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    _initialize(service, repo_name, feature_slot, "feature-c")
    commit_file(feature_slot.path, "shared.txt", "feature version\n", "feature touches shared")

    # Advance main with a conflicting change on shared.txt.
    commit_file(repo_path, "shared.txt", "main version\n", "main touches shared")

    result = service.sync(SelectorTarget(repo_name=repo_name, branch="feature-c"))
    assert "paused" in result.lower()
    op = service.db.get_operation(repo_name)
    assert op is not None
    assert op.status == "paused"

    # Abort cleans up and the slot returns to pool.
    abort_result = service.abort_operation(repo_name)
    assert "Aborted" in abort_result
    assert service.db.get_operation(repo_name) is None
