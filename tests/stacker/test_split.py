from __future__ import annotations

import pytest

from project_manager.paths import Paths
from project_manager.pool import add as add_mod
from project_manager.stacker import git as stacker_git
from project_manager.stacker import locate
from project_manager.stacker.models import OperationState, SelectorTarget
from project_manager.stacker.service import StackerService

from .conftest import TrackedStack, commit_file


def test_split_moves_later_commits_to_new_branch_stay(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`b` has commit b1; add b2, b3 then split at b2 with --stay.

    After: `b` keeps just b1, new branch `b-split` holds b2+b3,
    parent(b-split) = b, descendants of b (i.e. c) reparent onto b-split.
    """
    b_slot = tracked_stack.slots["b"]
    b2 = commit_file(b_slot.path, "b2.txt", "b2\n", "b: commit 2")
    commit_file(b_slot.path, "b3.txt", "b3\n", "b: commit 3")

    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    result = service.split(target, "b-split", b2, stay=True)
    assert "Split" in result

    b_split = service.db.get_branch(tracked_stack.repo_name, "b-split")
    assert b_split is not None
    assert b_split.parent_branch == "b"

    # c now parents off b-split, not b.
    c = service.db.get_branch(tracked_stack.repo_name, "c")
    assert c is not None
    assert c.parent_branch == "b-split"

    # b's tip went back to before b2 (one commit: b1).
    b_head = stacker_git.rev_parse(b_slot.path, "HEAD")
    b_tracked = service.db.get_branch(tracked_stack.repo_name, "b")
    assert b_tracked is not None
    # only one commit since managed_base (the original b1)
    commits = stacker_git.rev_list(
        b_slot.path,
        f"{b_tracked.managed_base_commit}..{b_head}",
    )
    assert len(commits) == 1


def test_split_refuses_at_first_commit(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """Can't split at the first commit — nothing stays on current."""
    first_commit = tracked_stack.commits["b"]
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    with pytest.raises(stacker_git.GitError, match="first commit"):
        service.split(target, "b-split", first_commit)


def test_split_refuses_off_branch_commit(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """A sha not in managed_base..HEAD is rejected."""
    other = tracked_stack.commits["d"]  # not on b
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    with pytest.raises(stacker_git.GitError, match="not on b"):
        service.split(target, "b-split", other)


def test_split_refuses_existing_new_name(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    b_slot = tracked_stack.slots["b"]
    b2 = commit_file(b_slot.path, "b2.txt", "b2\n", "b: commit 2")

    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    with pytest.raises(stacker_git.GitError, match="already exists"):
        service.split(target, "c", b2)


def test_split_refuses_dirty_worktree(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    b_slot = tracked_stack.slots["b"]
    b2 = commit_file(b_slot.path, "b2.txt", "b2\n", "b: commit 2")
    (b_slot.path / "b.txt").write_text("modified\n")

    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    with pytest.raises(stacker_git.GitError, match="Tracked changes"):
        service.split(target, "b-split", b2, stay=True)


def test_split_refuses_with_active_op(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    b_slot = tracked_stack.slots["b"]
    b2 = commit_file(b_slot.path, "b2.txt", "b2\n", "b: commit 2")
    service.db.put_operation(
        OperationState(
            repo_name=tracked_stack.repo_name,
            op_type="local_sync",
            status="paused",
            branch="b",
            parent_branch="a",
        )
    )
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    with pytest.raises(stacker_git.GitError, match="active"):
        service.split(target, "b-split", b2, stay=True)


def test_split_default_claims_free_slot_for_new_branch(
    pm_env: Paths,
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """Default split provisions a fresh slot for the new branch.

    The tracked_stack fixture claims every slot it creates, so we mint
    an extra one before split; that's the only place `_acquire` can
    safely check out `b-split`.
    """
    spare = add_mod.add(pm_env, tracked_stack.repo_name)
    b_slot = tracked_stack.slots["b"]
    b2 = commit_file(b_slot.path, "b2.txt", "b2\n", "b: commit 2")
    commit_file(b_slot.path, "b3.txt", "b3\n", "b: commit 3")

    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    result = service.split(target, "b-split", b2)
    assert "Split" in result
    assert "checked out in a fresh slot" in result

    landed = locate.locate_worktree(pm_env, tracked_stack.repo_name, "b-split")
    assert landed is not None
    assert landed == spare.path

    # b's original slot still has b (reset to b1), never displaced.
    assert stacker_git.current_branch(b_slot.path) == "b"


def test_split_default_degrades_when_pool_is_saturated(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """With no free slot, the split still succeeds but reports the gap.

    Every fixture slot is claimed; `_acquire` raises PoolExhaustedError.
    The split ref + DB row have already landed, so we don't unwind —
    we surface the pool state in the result so the user can `pm pool
    add` and then `pm project attach`.
    """
    b_slot = tracked_stack.slots["b"]
    b2 = commit_file(b_slot.path, "b2.txt", "b2\n", "b: commit 2")
    commit_file(b_slot.path, "b3.txt", "b3\n", "b: commit 3")

    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    result = service.split(target, "b-split", b2)
    assert "Split" in result
    assert "could not claim a slot" in result
    assert service.db.get_branch(tracked_stack.repo_name, "b-split") is not None
