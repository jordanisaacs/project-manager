"""Tests for the `tracked_branches.pr_url` cache.

Mirrors universe gitstack's `StackItem.pr` model: the URL of a created PR
is persisted on the branch and re-used on subsequent runs instead of
re-searching GitHub.
"""
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
    PushOptions,
    SelectorTarget,
    WorktreeInit,
)
from project_manager.stacker.service import StackerService

from .fakes import RecordingPRBackend


def _commit_one(slot_path: Path, name: str) -> None:
    (slot_path / name).write_text(f"{name}\n")
    stacker_git.git(slot_path, "add", name)
    stacker_git.git(slot_path, "-c", "user.email=t@e.com", "-c", "user.name=t",
                    "commit", "-m", f"add {name}")


def _config_upstream(slot_path: Path, branch: str) -> None:
    stacker_git.git(slot_path, "remote", "add", "origin-fake",
                    "git@github.com:acme/widgets.git", check=False)
    stacker_git.git(slot_path, "config", f"branch.{branch}.remote", "origin-fake")
    stacker_git.git(slot_path, "config", f"branch.{branch}.merge",
                    f"refs/heads/{branch}")


@pytest.fixture
def backend() -> RecordingPRBackend:
    return RecordingPRBackend()


@pytest.fixture
def service(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],  # noqa: ARG001 (creates demo repo)
    backend: RecordingPRBackend,
) -> StackerService:
    svc = StackerService(StackerDB(pm_env.stacker_db()), pm_env, pr_backend=backend)
    svc.db.set_config("demo", "pr.mode", "repo-pr")
    svc.db.set_config("demo", "pr.trunk", "main")
    return svc


@pytest.fixture
def feature_a(
    service: StackerService,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> slot_mod.Slot:
    repo_name, _repo_path = stacker_repo
    slot = three_slots[0]
    service.init_new_branch(
        WorktreeInit(
            repo_name=repo_name, worktree_path=slot.path, branch="feature-a",
            parent=ParentLocator(repo_name=repo_name, branch="main"),
        )
    )
    _commit_one(slot.path, "a.txt")
    _config_upstream(slot.path, "feature-a")
    monkeypatch.setattr(service, "_run_single_pp", lambda *_a, **_k: True)
    return slot


def test_create_populates_pr_url_on_branch(
    service: StackerService,
    feature_a: slot_mod.Slot,  # noqa: ARG001
) -> None:
    service.push(SelectorTarget(repo_name="demo", branch="feature-a"), PushOptions(draft=True))
    tracked = service.db.get_branch("demo", "feature-a")
    assert tracked is not None
    assert tracked.pr_url is not None
    assert tracked.pr_url.startswith("https://github.com/acme/widgets/pull/")


def test_second_run_is_cache_hit_no_search(
    service: StackerService,
    backend: RecordingPRBackend,
    feature_a: slot_mod.Slot,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service.push(SelectorTarget(repo_name="demo", branch="feature-a"), PushOptions(draft=True))

    # Second run: bump a counter from the backend's list_open_prs so we
    # can prove the cached URL path bypassed it entirely.
    search_calls: list[str] = []
    original = backend.list_open_prs

    def _count(
        repo: str, *, head: str | None = None, search: str | None = None,
    ) -> list[gh.PullRequest]:
        search_calls.append(search or head or "")
        return original(repo, head=head, search=search)

    monkeypatch.setattr(backend, "list_open_prs", _count)
    service.push(SelectorTarget(repo_name="demo", branch="feature-a"), PushOptions(draft=True))

    assert search_calls == [], f"cached URL should skip search, saw: {search_calls}"
    # Exactly one create + one or more edits (refresh) across both runs.
    assert len(backend.created) == 1
    assert len(backend.edited) >= 1


def test_search_hit_writes_url_back_for_future_runs(
    service: StackerService,
    backend: RecordingPRBackend,
    feature_a: slot_mod.Slot,  # noqa: ARG001
) -> None:
    """If the DB was dropped / populated by another tool, a search hit
    should persist the URL so subsequent runs go cache-first."""
    # Pretend a PR was opened by another tool (so it's in GitHub but not
    # in our DB). We do this by pre-populating the backend's head map.
    existing_pr = gh.PullRequest(
        number=99,
        url="https://github.com/acme/widgets/pull/99",
        title="preexisting",
        body="",
        head_ref_name="feature-a",
        base_ref_name="main",
        state="OPEN",
        is_draft=False,
    )
    backend.prs_by_head[("acme/widgets", "feature-a")] = existing_pr

    # Wipe the cached URL so the service has to search.
    tracked = service.db.get_branch("demo", "feature-a")
    assert tracked is not None
    assert tracked.pr_url is None

    service.push(SelectorTarget(repo_name="demo", branch="feature-a"), PushOptions(draft=True))
    after = service.db.get_branch("demo", "feature-a")
    assert after is not None
    # The existing PR should have been detected and its URL cached —
    # _create_or_update_current_pr updates PR 99 and stores its URL.
    assert after.pr_url == "https://github.com/acme/widgets/pull/99"
