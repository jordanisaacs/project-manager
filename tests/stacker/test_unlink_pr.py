"""Tests for `pm stacker unlink-pr` — clearing the cached pr_state."""
from __future__ import annotations

from project_manager.stacker.models import PRState
from project_manager.stacker.service import StackerService


def _pr(repo: str, branch: str) -> PRState:
    return PRState(
        repo_name=repo,
        branch=branch,
        pr_url=f"https://github.com/acme/{repo}/pull/1",
        pr_number=1,
        state="OPEN",
    )


def test_delete_pr_state_clears_linked_branch(service: StackerService) -> None:
    service.db.upsert_pr_state(_pr("demo", "feat"))
    assert service.delete_pr_state("demo", "feat") is True
    assert service.db.get_pr_state("demo", "feat") is None


def test_delete_pr_state_is_idempotent(service: StackerService) -> None:
    assert service.delete_pr_state("demo", "feat") is False


def test_delete_all_pr_state_is_scoped_to_repo(service: StackerService) -> None:
    service.db.upsert_pr_state(_pr("demo", "a"))
    service.db.upsert_pr_state(_pr("demo", "b"))
    service.db.upsert_pr_state(_pr("other", "c"))
    assert service.delete_all_pr_state("demo") == 2
    assert service.db.get_pr_state("demo", "a") is None
    assert service.db.get_pr_state("demo", "b") is None
    assert service.db.get_pr_state("other", "c") is not None
