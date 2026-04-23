from __future__ import annotations

import pytest

from project_manager.stacker import git as stacker_git
from project_manager.stacker.models import OperationState, SelectorTarget, TrackedBranch
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


def test_rename_preserves_pr_url_cache(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """The pr_url cache travels with the branch across rename."""
    b = service.db.get_branch(tracked_stack.repo_name, "b")
    assert b is not None
    service.db.upsert_branch(
        TrackedBranch(
            repo_name=b.repo_name,
            branch=b.branch,
            parent_repo_name=b.parent_repo_name,
            parent_branch=b.parent_branch,
            managed_base_commit=b.managed_base_commit,
            last_synced_parent_commit=b.last_synced_parent_commit,
            last_clean_head=b.last_clean_head,
            pr_url="https://github.com/acme/widgets/pull/42",
        )
    )
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    service.rename(target, "b-renamed")

    renamed = service.db.get_branch(tracked_stack.repo_name, "b-renamed")
    assert renamed is not None
    assert renamed.pr_url == "https://github.com/acme/widgets/pull/42"
