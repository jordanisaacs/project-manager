"""Tests for the PR cache surface: unlink/refresh + the track-time PR auto-link."""
from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.stacker import gh
from project_manager.stacker import git as stacker_git
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import (
    ParentLocator,
    PRState,
    SelectorTarget,
)
from project_manager.stacker.service import StackerService

from .fakes import RecordingPRBackend


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


# --- refresh + track auto-link --------------------------------------------


@pytest.fixture
def backend() -> RecordingPRBackend:
    return RecordingPRBackend()


@pytest.fixture
def pr_service(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],  # noqa: ARG001 (creates demo repo)
    backend: RecordingPRBackend,
) -> StackerService:
    svc = StackerService(StackerDB(pm_env.stacker_db()), pm_env, pr_backend=backend)
    svc.db.set_config("demo", "pr.mode", "repo-pr")
    svc.db.set_config("demo", "pr.trunk", "main")
    return svc


def _config_upstream(slot_path: Path, branch: str) -> None:
    stacker_git.git(
        slot_path, "remote", "add", "origin-fake",
        "git@github.com:acme/widgets.git", check=False,
    )
    stacker_git.git(
        slot_path, "config", f"branch.{branch}.remote", "origin-fake",
    )
    stacker_git.git(
        slot_path, "config", f"branch.{branch}.merge",
        f"refs/heads/{branch}",
    )


def _seed_pr(
    backend: RecordingPRBackend, repo: str, head: str, *, number: int = 99,
) -> gh.PullRequest:
    pr = gh.PullRequest(
        number=number,
        url=f"https://github.com/acme/widgets/pull/{number}",
        title="preexisting",
        body="",
        head_ref_name=head,
        base_ref_name="main",
        state="OPEN",
        is_draft=False,
    )
    backend.prs_by_head[(repo, head)] = pr
    return pr


def test_track_links_existing_open_pr(
    pr_service: StackerService,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    backend: RecordingPRBackend,
) -> None:
    """`create --replace` should auto-discover an open PR for the adopted branch."""
    repo_name, repo_path = stacker_repo
    stacker_git.git(repo_path, "branch", "feature-existing", "main")
    s = three_slots[0]
    stacker_git.git(s.path, "checkout", "feature-existing")
    _config_upstream(s.path, "feature-existing")
    _seed_pr(backend, "acme/widgets", "feature-existing", number=42)

    pr_service.track(
        SelectorTarget(repo_name=repo_name, branch="feature-existing"),
        ParentLocator(repo_name=repo_name, branch="main"),
    )
    cached = pr_service.db.get_pr_state(repo_name, "feature-existing")
    assert cached is not None
    assert cached.pr_url == "https://github.com/acme/widgets/pull/42"


def test_track_silently_skips_when_no_pr_exists(
    pr_service: StackerService,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    """No PR on GitHub → adoption still succeeds, just no cache row."""
    repo_name, repo_path = stacker_repo
    stacker_git.git(repo_path, "branch", "feature-orphan", "main")
    s = three_slots[0]
    stacker_git.git(s.path, "checkout", "feature-orphan")
    _config_upstream(s.path, "feature-orphan")

    tracked = pr_service.track(
        SelectorTarget(repo_name=repo_name, branch="feature-orphan"),
        ParentLocator(repo_name=repo_name, branch="main"),
    )
    assert tracked.parent_branch == "main"
    assert pr_service.db.get_pr_state(repo_name, "feature-orphan") is None


def test_refresh_pr_rediscovers_after_unlink(
    pr_service: StackerService,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    backend: RecordingPRBackend,
) -> None:
    """`pm stacker pr refresh` re-runs discovery even when the cache is empty."""
    repo_name, repo_path = stacker_repo
    stacker_git.git(repo_path, "branch", "feature-relink", "main")
    s = three_slots[0]
    stacker_git.git(s.path, "checkout", "feature-relink")
    _config_upstream(s.path, "feature-relink")

    pr_service.track(
        SelectorTarget(repo_name=repo_name, branch="feature-relink"),
        ParentLocator(repo_name=repo_name, branch="main"),
    )
    # Adoption ran first with no PR on GitHub yet → cache stays empty.
    assert pr_service.db.get_pr_state(repo_name, "feature-relink") is None

    # PR is opened later (e.g. via `git pp` outside stacker).
    _seed_pr(backend, "acme/widgets", "feature-relink", number=7)

    pr = pr_service.refresh_pr(
        SelectorTarget(repo_name=repo_name, branch="feature-relink"),
    )
    assert pr is not None
    assert pr.url == "https://github.com/acme/widgets/pull/7"
    cached = pr_service.db.get_pr_state(repo_name, "feature-relink")
    assert cached is not None
    assert cached.pr_url == "https://github.com/acme/widgets/pull/7"


def test_refresh_pr_clears_stale_cache(
    pr_service: StackerService,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    backend: RecordingPRBackend,
) -> None:
    """Stale cached PR URL → refresh drops the row and re-discovers via search.

    Without the unlink-then-search step, find_pr's cache-first path would
    keep returning the stale URL.
    """
    repo_name, repo_path = stacker_repo
    stacker_git.git(repo_path, "branch", "feature-replaced", "main")
    s = three_slots[0]
    stacker_git.git(s.path, "checkout", "feature-replaced")
    _config_upstream(s.path, "feature-replaced")
    pr_service.track(
        SelectorTarget(repo_name=repo_name, branch="feature-replaced"),
        ParentLocator(repo_name=repo_name, branch="main"),
    )
    # Pre-seed a stale cache entry (URL doesn't match any seeded PR).
    pr_service.db.upsert_pr_state(
        PRState(
            repo_name=repo_name,
            branch="feature-replaced",
            pr_url="https://github.com/acme/widgets/pull/1",
            pr_number=1,
            state="OPEN",
        ),
    )
    _seed_pr(backend, "acme/widgets", "feature-replaced", number=200)

    pr = pr_service.refresh_pr(
        SelectorTarget(repo_name=repo_name, branch="feature-replaced"),
    )
    assert pr is not None
    assert pr.url == "https://github.com/acme/widgets/pull/200"
