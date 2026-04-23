from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.stacker import gh, git
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import PRMode, RepoPRConfig, TrackedBranch
from project_manager.stacker.pr.resolve import pr_base_for_current_branch
from project_manager.stacker.service import StackerService

from .fakes import RecordingPRBackend


@pytest.fixture
def service(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],  # noqa: ARG001 (creates repo on disk)
) -> StackerService:
    return StackerService(
        StackerDB(pm_env.stacker_db()), pm_env, pr_backend=RecordingPRBackend(),
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
