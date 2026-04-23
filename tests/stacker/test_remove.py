from __future__ import annotations

import pytest

from project_manager.stacker import git as stacker_git
from project_manager.stacker.models import OperationState, SelectorTarget
from project_manager.stacker.service import StackerService

from .conftest import TrackedStack


def test_remove_default_deletes_branch_and_reparents_children(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """Default `remove b` deletes `b` and reparents `c`, `d` onto `b`'s parent `a`."""
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    result = service.remove(target)
    assert "Removed" in result

    assert service.db.get_branch(tracked_stack.repo_name, "b") is None
    c = service.db.get_branch(tracked_stack.repo_name, "c")
    assert c is not None
    assert c.parent_branch == "a"

    # git branch -D actually removed the ref.
    existed = stacker_git.branch_exists(tracked_stack.repo_path, "b")
    assert not existed


def test_remove_keep_branch_preserves_git_ref(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`--keep-branch` matches the old `untrack` behavior: ref survives."""
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    service.remove(target, keep_branch=True)

    assert service.db.get_branch(tracked_stack.repo_name, "b") is None
    assert stacker_git.branch_exists(tracked_stack.repo_path, "b")


def test_remove_parent_cascade_removes_ancestors(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`--parent` walks up the chain and removes every tracked ancestor.

    After `remove c --parent`, only `d` (reparented to main) and the
    untracked `main` root remain.
    """
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="c")
    result = service.remove(target, parent_cascade=True, keep_branch=True)
    assert "Removed" in result
    assert service.db.get_branch(tracked_stack.repo_name, "c") is None
    assert service.db.get_branch(tracked_stack.repo_name, "b") is None
    assert service.db.get_branch(tracked_stack.repo_name, "a") is None
    d = service.db.get_branch(tracked_stack.repo_name, "d")
    assert d is not None
    assert d.parent_branch == "main"


def test_remove_refuses_with_active_op(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
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
        service.remove(target)


def test_remove_errors_on_untracked_branch(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="ghost")
    with pytest.raises(stacker_git.GitError, match="not tracked"):
        service.remove(target)
