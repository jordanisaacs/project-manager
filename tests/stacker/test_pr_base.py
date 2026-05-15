from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.stacker import gh, git
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import PRMode, PRState, RepoPRConfig, TrackedBranch
from project_manager.stacker.pr.resolve import pr_base_for_current_branch
from project_manager.stacker.service import StackerService

from .fakes import RecordingPRBackend


@pytest.fixture
def backend() -> RecordingPRBackend:
    return RecordingPRBackend()


@pytest.fixture
def service(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],  # noqa: ARG001 (creates repo on disk)
    backend: RecordingPRBackend,
) -> StackerService:
    return StackerService(
        StackerDB(pm_env.stacker_db()),
        pm_env,
        pr_backend=backend,
    )


def _tracked(branch: str, parent_branch: str) -> TrackedBranch:
    return TrackedBranch(
        repo_name="demo",
        branch=branch,
        parent_repo_name="demo",
        parent_branch=parent_branch,
        managed_base_commit="0" * 40,
        last_synced_parent_commit=None,
        last_clean_head=None,
    )


def _config(mode: PRMode) -> RepoPRConfig:
    return RepoPRConfig(
        repo_name="demo",
        mode=mode,
        trunk_branch="main",
        target_repo="acme/widgets",
    )


def _repo_info() -> gh.RepoInfo:
    return gh.RepoInfo(name_with_owner="acme/widgets", owner="acme", name="widgets")


def test_repo_pr_mode_always_returns_trunk(service: StackerService) -> None:
    # Even with a non-trunk parent, repo-pr short-circuits to trunk.
    tracked = _tracked("feat-b", "feat-a")
    base = pr_base_for_current_branch(service.ctx, tracked, _config("repo-pr"), _repo_info())
    assert base == "main"


def test_pr_pr_mode_on_trunk_parent_returns_trunk(service: StackerService) -> None:
    tracked = _tracked("feat-a", "main")
    base = pr_base_for_current_branch(service.ctx, tracked, _config("pr-pr"), _repo_info())
    assert base == "main"


def test_pr_pr_mode_with_untracked_parent_errors(service: StackerService) -> None:
    tracked = _tracked("feat-b", "feat-a")
    with pytest.raises(git.GitError, match="must already have an open PR"):
        pr_base_for_current_branch(service.ctx, tracked, _config("pr-pr"), _repo_info())


def test_pr_pr_mode_uses_parent_pr_head_ref_when_parent_not_in_worktree(
    service: StackerService,
    backend: RecordingPRBackend,
) -> None:
    # Repro of the live bug: parent is tracked and has an open PR, but is not
    # checked out in any worktree (e.g. its slot was released after pm pushed
    # it earlier in `pm stacker push`). Resolving the PR base must not require
    # the parent's worktree — the parent's remote ref is already carried on
    # `find_open_pr`'s returned `gh.PullRequest.head_ref_name`.
    parent = _tracked("feat-a", "main")
    child = _tracked("feat-b", "feat-a")
    service.db.upsert_branch(parent)
    service.db.upsert_branch(child)

    parent_pr_url = "https://github.com/acme/widgets/pull/42"
    backend.prs_by_head[("acme/widgets", "jordan-isaacs_data/feat-a")] = gh.PullRequest(
        number=42,
        url=parent_pr_url,
        title="parent pr",
        body="",
        head_ref_name="jordan-isaacs_data/feat-a",
        base_ref_name="main",
        state="OPEN",
        is_draft=False,
    )
    service.db.upsert_pr_state(
        PRState(
            repo_name="demo",
            branch="feat-a",
            pr_url=parent_pr_url,
            state="OPEN",
        ),
    )

    base = pr_base_for_current_branch(
        service.ctx,
        child,
        _config("pr-pr"),
        _repo_info(),
    )
    assert base == "jordan-isaacs_data/feat-a"


def test_pr_pr_mode_errors_if_parent_pr_has_empty_head_ref(
    service: StackerService,
    backend: RecordingPRBackend,
) -> None:
    # Defensive: gh.PullRequest.head_ref_name is `str` not `str | None`, but
    # if a backend ever returns an open PR with an empty ref we must surface
    # a clear error instead of silently emitting `base=""` to `gh pr edit`.
    parent = _tracked("feat-a", "main")
    child = _tracked("feat-b", "feat-a")
    service.db.upsert_branch(parent)
    service.db.upsert_branch(child)

    parent_pr_url = "https://github.com/acme/widgets/pull/43"
    backend.prs_by_head[("acme/widgets", "")] = gh.PullRequest(
        number=43,
        url=parent_pr_url,
        title="parent pr",
        body="",
        head_ref_name="",
        base_ref_name="main",
        state="OPEN",
        is_draft=False,
    )
    service.db.upsert_pr_state(
        PRState(
            repo_name="demo",
            branch="feat-a",
            pr_url=parent_pr_url,
            state="OPEN",
        ),
    )

    with pytest.raises(git.GitError, match="no head ref"):
        pr_base_for_current_branch(
            service.ctx,
            child,
            _config("pr-pr"),
            _repo_info(),
        )
