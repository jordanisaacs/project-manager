from __future__ import annotations

import pytest

from project_manager.stacker import git as stacker_git
from project_manager.stacker.models import ParentLocator, SelectorTarget
from project_manager.stacker.service import StackerService

from .conftest import TrackedStack, commit_file


def test_reparent_moves_branch_onto_new_parent(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """Reparent `c` from `b` to `a`.

    DB row for `c` now points at `a`; descendants of `c` (`d`) also
    re-cherry-pick onto the new lineage.
    """
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="c")
    new_parent = ParentLocator(repo_name=tracked_stack.repo_name, branch="a")
    result = service.reparent(target, new_parent)
    assert "Sync complete." in result

    c = service.db.get_branch(tracked_stack.repo_name, "c")
    assert c is not None
    assert c.parent_branch == "a"
    d = service.db.get_branch(tracked_stack.repo_name, "d")
    assert d is not None
    assert d.parent_branch == "c"


def test_reparent_refuses_cycle(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """Reparenting `b` onto `d` (its own descendant) must error."""
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    new_parent = ParentLocator(repo_name=tracked_stack.repo_name, branch="d")
    with pytest.raises(stacker_git.GitError, match="cycle"):
        service.reparent(target, new_parent)


def test_reparent_refuses_self(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    new_parent = ParentLocator(repo_name=tracked_stack.repo_name, branch="b")
    with pytest.raises(stacker_git.GitError, match="own parent"):
        service.reparent(target, new_parent)


def test_reparent_errors_on_nonexistent_parent(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="c")
    new_parent = ParentLocator(repo_name=tracked_stack.repo_name, branch="ghost")
    with pytest.raises(stacker_git.GitError, match="not found"):
        service.reparent(target, new_parent)


def test_reparent_cross_repo_rejected(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="c")
    new_parent = ParentLocator(repo_name="other-repo", branch="a")
    with pytest.raises(stacker_git.GitError, match="Cross-repo"):
        service.reparent(target, new_parent)


def test_reparent_pauses_on_conflict(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """A cherry-pick conflict during reparent pauses the op.

    `c` writes `shared.txt = "c"`, `a` writes `shared.txt = "a"` — the
    file conflicts; reparenting `c` onto `a` must land at a paused op
    so the user can resolve and run `sync --continue`.
    """
    a_slot = tracked_stack.slots["a"]
    c_slot = tracked_stack.slots["c"]
    commit_file(a_slot.path, "shared.txt", "a\n", "a: shared")
    commit_file(c_slot.path, "shared.txt", "c\n", "c: shared")

    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="c")
    new_parent = ParentLocator(repo_name=tracked_stack.repo_name, branch="a")
    result = service.reparent(target, new_parent)
    assert "paused" in result.lower()

    op = service.db.get_operation(tracked_stack.repo_name)
    assert op is not None
    assert op.status == "paused"

    # Aborting cleans up.
    service.abort_operation(tracked_stack.repo_name)
    assert service.db.get_operation(tracked_stack.repo_name) is None
