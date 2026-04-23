from __future__ import annotations

import pytest

from project_manager.stacker import git as stacker_git
from project_manager.stacker.models import OperationState, PRState, SelectorTarget
from project_manager.stacker.service import StackerService

from .conftest import TrackedStack


def test_rename_rewrites_db_and_git_ref(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """Rename `b` to `b2`: DB row + git branch ref both follow."""
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    result = service.rename(target, "b2")
    assert "Renamed b -> b2" in result

    assert service.db.get_branch(tracked_stack.repo_name, "b") is None
    b2 = service.db.get_branch(tracked_stack.repo_name, "b2")
    assert b2 is not None

    assert stacker_git.branch_exists(tracked_stack.repo_path, "b2")
    assert not stacker_git.branch_exists(tracked_stack.repo_path, "b")


def test_rename_reparents_children(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """After `rename b -> b2`, `c`'s DB row must point at `b2`."""
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    service.rename(target, "b2")
    c = service.db.get_branch(tracked_stack.repo_name, "c")
    assert c is not None
    assert c.parent_branch == "b2"


def test_rename_same_name_is_noop(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    result = service.rename(target, "b")
    assert "already named" in result


def test_rename_refuses_existing_branch(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    with pytest.raises(stacker_git.GitError, match="already exists"):
        service.rename(target, "c")


def test_rename_refuses_with_active_op(
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
        service.rename(target, "b2")


def test_rename_preserves_pr_state_cache(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """The pr_state cache travels with the branch across rename."""
    service.db.upsert_pr_state(
        PRState(
            repo_name=tracked_stack.repo_name,
            branch="b",
            pr_url="https://github.com/acme/widgets/pull/42",
            state="OPEN",
            pr_number=42,
        ),
    )
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    service.rename(target, "b-renamed")

    # The old pr_state row was deleted when `b` was removed; the rename
    # path re-inserted it under the new branch name.
    assert service.db.get_pr_state(tracked_stack.repo_name, "b") is None
    renamed_pr = service.db.get_pr_state(tracked_stack.repo_name, "b-renamed")
    assert renamed_pr is not None
    assert renamed_pr.pr_url == "https://github.com/acme/widgets/pull/42"
