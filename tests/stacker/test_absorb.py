from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.pool import slot as slot_mod
from project_manager.stacker import git as stacker_git
from project_manager.stacker.models import (
    OperationState,
    ParentLocator,
    SelectorTarget,
    WorktreeInit,
)
from project_manager.stacker.service import StackerService

from .conftest import commit_file


def _init_on_main(
    service: StackerService, repo_name: str, slot: slot_mod.Slot, branch: str
) -> None:
    """Create a tracked branch off main in `slot`."""
    service.init_new_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=slot.path,
            branch=branch,
            parent=ParentLocator(repo_name=repo_name, branch="main"),
        )
    )


def _head(path: Path) -> str:
    return stacker_git.rev_parse(path, "HEAD")


def _count_commits(path: Path, revspec: str) -> int:
    return stacker_git.rev_count(path, revspec)


# ---------------------------------------------------------------------------
# Case 1: ff absorb — parent at child's base, child has commits.
# ---------------------------------------------------------------------------


def test_absorb_ff_advances_trunk_and_leaves_child_alone(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    _init_on_main(service, repo_name, feature_slot, "feature")
    commit_file(feature_slot.path, "a.txt", "a\n", "feat: a")
    commit_file(feature_slot.path, "b.txt", "b\n", "feat: b")
    child_head_before = _head(feature_slot.path)
    main_before = _head(repo_path)

    result = service.absorb(
        SelectorTarget(repo_name=repo_name, branch="feature"),
    )

    assert "Absorb complete." in result
    # Main advanced by exactly the two cherry-picked commits.
    main_after = _head(repo_path)
    assert main_after != main_before
    assert _count_commits(repo_path, f"{main_before}..{main_after}") == 2
    # Child unchanged.
    assert _head(feature_slot.path) == child_head_before
    # No lingering operation.
    assert service.db.get_operation(repo_name) is None
    # Trunk isn't tracked, so no row for main should be created.
    assert service.db.get_branch(repo_name, "main") is None


# ---------------------------------------------------------------------------
# Case 2: parent has diverged (no conflict) — parent gets its own commits
# plus the cherry-picked child commits.
# ---------------------------------------------------------------------------


def test_absorb_with_diverged_parent_no_conflict(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    _init_on_main(service, repo_name, feature_slot, "feature")
    commit_file(feature_slot.path, "child-only.txt", "c\n", "feat: c")

    # Parent advances on an unrelated file — no conflict expected.
    main_bump = commit_file(repo_path, "main-only.txt", "m\n", "main: bump")

    result = service.absorb(
        SelectorTarget(repo_name=repo_name, branch="feature"),
    )

    assert "Absorb complete." in result
    main_after = _head(repo_path)
    # main_bump is still reachable from main_after (parent's own commit kept).
    assert stacker_git.is_ancestor(repo_path, main_bump, main_after)
    # The cherry-picked child commit is now on main (new SHA, same file).
    assert (repo_path / "child-only.txt").read_text() == "c\n"


# ---------------------------------------------------------------------------
# Case 3: genuine conflict during absorb — pauses, abort rolls back.
# ---------------------------------------------------------------------------


def test_absorb_conflict_pauses_and_abort_resets_parent(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    _init_on_main(service, repo_name, feature_slot, "feature")
    commit_file(feature_slot.path, "shared.txt", "child version\n", "feat: shared")

    # Parent writes a different version of the same file.
    commit_file(repo_path, "shared.txt", "main version\n", "main: shared")
    main_before = _head(repo_path)

    result = service.absorb(
        SelectorTarget(repo_name=repo_name, branch="feature"),
    )
    assert "paused" in result.lower()
    op = service.db.get_operation(repo_name)
    assert op is not None
    assert op.op_type == "local_absorb"
    assert op.status == "paused"

    abort_result = service.abort_operation(repo_name)
    assert "Aborted" in abort_result
    assert service.db.get_operation(repo_name) is None
    # Parent reset to exactly where it was before absorb started.
    assert _head(repo_path) == main_before


# ---------------------------------------------------------------------------
# Case 4: --continue drives a paused absorb to completion.
# ---------------------------------------------------------------------------


def test_absorb_continue_after_manual_resolution(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    _init_on_main(service, repo_name, feature_slot, "feature")
    commit_file(feature_slot.path, "shared.txt", "child version\n", "feat: shared")
    commit_file(repo_path, "shared.txt", "main version\n", "main: shared")

    paused = service.absorb(
        SelectorTarget(repo_name=repo_name, branch="feature"),
    )
    assert "paused" in paused.lower()

    # User resolves by picking the child version in the parent slot.
    (repo_path / "shared.txt").write_text("child version\n")
    stacker_git.git(repo_path, "add", "shared.txt")

    finished = service.continue_operation(repo_name)
    assert "Absorb complete." in finished
    assert service.db.get_operation(repo_name) is None


# ---------------------------------------------------------------------------
# Case 5: nothing to absorb short-circuits without touching state.
# ---------------------------------------------------------------------------


def test_absorb_nothing_to_do(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    _init_on_main(service, repo_name, feature_slot, "feature")
    main_before = _head(repo_path)

    result = service.absorb(
        SelectorTarget(repo_name=repo_name, branch="feature"),
    )
    assert "Nothing to absorb" in result
    assert _head(repo_path) == main_before
    assert service.db.get_operation(repo_name) is None


# ---------------------------------------------------------------------------
# Case 6: absorb into trunk leaves no DB row for trunk.
# ---------------------------------------------------------------------------


def test_absorb_into_trunk_does_not_track_trunk(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, _repo_path = stacker_repo
    feature_slot = three_slots[0]
    _init_on_main(service, repo_name, feature_slot, "feature")
    commit_file(feature_slot.path, "f.txt", "f\n", "feat: f")

    service.absorb(SelectorTarget(repo_name=repo_name, branch="feature"))

    # Trunk must remain untracked — absorb never creates a TrackedBranch row
    # for main just because commits landed on it.
    assert service.db.get_branch(repo_name, "main") is None


# ---------------------------------------------------------------------------
# Case 7: current branch is not tracked.
# ---------------------------------------------------------------------------


def test_absorb_rejects_untracked_branch(
    stacker_repo: tuple[str, Path],
    service: StackerService,
) -> None:
    repo_name, _ = stacker_repo
    with pytest.raises(stacker_git.GitError):
        service.absorb(SelectorTarget(repo_name=repo_name, branch="nope"))


# ---------------------------------------------------------------------------
# Case 8: another op is already active.
# ---------------------------------------------------------------------------


def test_absorb_rejects_when_op_active(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, _ = stacker_repo
    feature_slot = three_slots[0]
    _init_on_main(service, repo_name, feature_slot, "feature")
    commit_file(feature_slot.path, "f.txt", "f\n", "feat: f")

    service.db.put_operation(
        OperationState(
            repo_name=repo_name, op_type="local_sync", status="paused",
            branch="feature",
        )
    )

    with pytest.raises(stacker_git.GitError, match="Another stacker operation"):
        service.absorb(SelectorTarget(repo_name=repo_name, branch="feature"))


# ---------------------------------------------------------------------------
# Case 9: trunk has sibling tracked children — absorb on one doesn't
# invalidate the sibling; sync on both converges cleanly.
# ---------------------------------------------------------------------------


def test_absorb_leaves_sibling_tracked_children_intact(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    a_slot, b_slot = three_slots[0], three_slots[1]
    _init_on_main(service, repo_name, a_slot, "a")
    _init_on_main(service, repo_name, b_slot, "b")
    commit_file(a_slot.path, "a.txt", "a\n", "a: first")
    commit_file(b_slot.path, "b.txt", "b\n", "b: first")

    b_row_before = service.db.get_branch(repo_name, "b")
    assert b_row_before is not None
    main_before = _head(repo_path)

    service.absorb(SelectorTarget(repo_name=repo_name, branch="a"))

    # Main advanced.
    assert _head(repo_path) != main_before
    # Sibling B's row is byte-for-byte unchanged.
    b_row_after = service.db.get_branch(repo_name, "b")
    assert b_row_after is not None
    assert b_row_after == b_row_before
    # Sibling B's managed_base is still a reachable ancestor of new main.
    assert stacker_git.is_ancestor(
        repo_path, b_row_after.managed_base_commit, _head(repo_path),
    )

    # Sync on B rebases it onto new main. Use skip_descendants so the walk
    # stays on B itself (B has no descendants but this keeps the scope tight).
    sync_b = service.sync(SelectorTarget(repo_name=repo_name, branch="b"))
    assert "Sync complete." in sync_b

    # Sync on A drops all its (now-duplicated) commits via the patch-id filter.
    sync_a = service.sync(SelectorTarget(repo_name=repo_name, branch="a"))
    assert "Sync complete." in sync_a
    a_after = service.db.get_branch(repo_name, "a")
    assert a_after is not None
    # A's managed base has caught up to the new main tip.
    assert a_after.managed_base_commit == _head(repo_path)


# ---------------------------------------------------------------------------
# Case 10: sync drops patch-id duplicates silently (the new filter).
# Guards against a future regression where sync_plan is reverted to a plain
# rev-list and the absorb → sync flow starts prompting for conflicts.
# ---------------------------------------------------------------------------


def test_sync_drops_patchid_duplicates_without_pause(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    _init_on_main(service, repo_name, feature_slot, "feature")
    commit_file(feature_slot.path, "x.txt", "x\n", "feat: x")

    # Mimic the post-absorb shape: the same patch lands on main under a
    # different SHA. git cherry-pick on main via the pm repo (which has main
    # checked out) is the simplest way to fake this.
    feature_head = _head(feature_slot.path)
    stacker_git.git(repo_path, "cherry-pick", feature_head)

    result = service.sync(SelectorTarget(repo_name=repo_name, branch="feature"))
    # Must complete without pausing — the patch-id filter should drop the
    # child's commit before git cherry-pick ever attempts it.
    assert "paused" not in result.lower()
    assert "Sync complete." in result


# ---------------------------------------------------------------------------
# Case 11: the patch-id filter doesn't hide genuine content conflicts.
# ---------------------------------------------------------------------------


def test_sync_still_surfaces_real_conflicts(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    _init_on_main(service, repo_name, feature_slot, "feature")
    commit_file(feature_slot.path, "shared.txt", "child side\n", "feat: shared")

    # Different content on the same file — different patch-id, real conflict.
    commit_file(repo_path, "shared.txt", "main side\n", "main: shared")

    result = service.sync(SelectorTarget(repo_name=repo_name, branch="feature"))
    assert "paused" in result.lower()

    service.abort_operation(repo_name)
