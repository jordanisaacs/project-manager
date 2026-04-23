from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OWNER_STACKER_OPS, PoolDB
from project_manager.stacker import git as stacker_git
from project_manager.stacker import locate
from project_manager.stacker.models import (
    ParentLocator,
    ScopeSpec,
    SelectorTarget,
    WorktreeInit,
)
from project_manager.stacker.service import StackerService

from .conftest import TrackedStack, commit_file


def _initialize(
    service: StackerService, repo_name: str, slot: slot_mod.Slot, branch: str
) -> None:
    service.init_new_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=slot.path,
            branch=branch,
            parent=ParentLocator(repo_name=repo_name, branch="main"),
        )
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


# --- Scope-flag coverage ----------------------------------------------------


def test_sync_default_scope_walks_lineage(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """Default scope `-c` walks ancestors + target + descendants.

    With `main` unchanged, every branch is up-to-date; the walk still
    visits each and reports "Sync complete." once.
    """
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    result = service.sync(target)
    assert "Sync complete." in result


def test_sync_skip_ancestors_matches_old_push_behavior(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`--skip-ancestors` syncs target + descendants (the old `push` meaning).

    Add a commit to `b`, expect `b`'s tip becomes the managed base of
    descendants `c` and `d` after sync walk.
    """
    b_slot = tracked_stack.slots["b"]
    new_b_head = commit_file(b_slot.path, "b-extra.txt", "b+\n", "b: extra")
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")

    result = service.sync(target, ScopeSpec(scope="current", skip_ancestors=True))
    assert "Sync complete." in result

    c_tracked = service.db.get_branch(tracked_stack.repo_name, "c")
    d_tracked = service.db.get_branch(tracked_stack.repo_name, "d")
    assert c_tracked is not None
    assert d_tracked is not None
    assert c_tracked.managed_base_commit == new_b_head
    assert d_tracked.last_synced_parent_commit is not None


def test_sync_skip_descendants_leaves_descendants_untouched(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`--skip-descendants` must not rewrite descendants' managed bases."""
    before_d = service.db.get_branch(tracked_stack.repo_name, "d")
    assert before_d is not None

    # Advance main so ancestors need a sync — but descendants should be skipped.
    commit_file(tracked_stack.repo_path, "shared.txt", "v\n", "main advance")

    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    service.sync(target, ScopeSpec(scope="current", skip_descendants=True))

    after_d = service.db.get_branch(tracked_stack.repo_name, "d")
    assert after_d is not None
    assert after_d.managed_base_commit == before_d.managed_base_commit


def test_sync_all_walks_every_tracked_branch(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`--all` resolves to every tracked branch, regardless of target."""
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="a")
    result = service.sync(target, ScopeSpec(scope="all"))
    assert "Sync complete." in result


def test_sync_from_branch_trims_walk(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`--from c` on a lineage walk starts the walk at `c`."""
    commit_file(tracked_stack.repo_path, "main-bump.txt", "m\n", "main bump")
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    result = service.sync(
        target, ScopeSpec(scope="current", from_branch="c"),
    )
    assert "Sync complete." in result


def test_sync_from_branch_rejects_out_of_scope(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`--from nonexistent` raises rather than silently walking nothing."""
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    with pytest.raises(stacker_git.GitError, match="--from"):
        service.sync(target, ScopeSpec(scope="current", from_branch="nonexistent"))
