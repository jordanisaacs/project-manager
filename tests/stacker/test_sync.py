from __future__ import annotations

from pathlib import Path

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool import worktree as wt
from project_manager.stacker import locate, ops_slot
from project_manager.stacker.models import ParentLocator, SelectorTarget
from project_manager.stacker.service import StackerService

from .conftest import commit_file


def _initialize(service: StackerService, repo_name: str, slot: slot_mod.Slot, branch: str) -> None:
    parent = ParentLocator(repo_name=repo_name, branch="main")
    service.initialize_worktree(
        repo_name=repo_name,
        worktree_path=slot.path,
        branch=branch,
        create_branch=True,
        parent=parent,
    )


def _advance_main(repo_path: Path) -> str:
    """Commit directly to main in the pm 'repo' (which has main checked out)."""
    return commit_file(repo_path, "extra.txt", "extra\n", "extra")


def test_sync_against_live_worktree(
    pm_env: Paths,  # noqa: ARG001
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
    # The branch's last_clean_head should now be set, and parent base updated.
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
    wt._git(feature_slot.path, "checkout", "--detach", "HEAD")
    # Free the slot so ops_slot can claim another one (or this one).
    slot_mod.release(feature_slot) if feature_slot.owner_path.exists() else None
    assert locate.locate_worktree(pm_env, repo_name, "feature-b") is None

    _advance_main(repo_path)

    result = service.sync(SelectorTarget(repo_name=repo_name, branch="feature-b"))
    assert "Sync complete." in result
    # After clean completion, the ops slot should be released (detached HEAD, .owner gone).
    for slot in slot_mod.list_slots(pm_env, repo_name):
        if slot.owner_target() is not None:
            # If any slot is still owned, it should be by a project (not the ops marker).
            assert slot.owner_target().resolve() != pm_env.stacker_ops_marker().resolve()


def test_sync_paused_on_conflict_keeps_slot_claimed(
    pm_env: Paths,
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
    # Operation row persists.
    op = service.db.get_operation(repo_name)
    assert op is not None
    assert op.status == "paused"

    # Abort cleans up and the slot returns to pool.
    abort_result = service.abort_operation(repo_name)
    assert "Aborted" in abort_result
    assert service.db.get_operation(repo_name) is None
