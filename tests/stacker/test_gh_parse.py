from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.stacker import git as stacker_git
from project_manager.stacker.db import StackerDB
from project_manager.stacker.git import parse_github_slug
from project_manager.stacker.models import ParentLocator, WorktreeInit
from project_manager.stacker.pr.resolve import head_repo_for_branch
from project_manager.stacker.service import StackerService

from .fakes import RecordingPRBackend


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:acme/widgets.git", "acme/widgets"),
        ("git@github.com:acme/widgets-dev.git", "acme/widgets-dev"),
        ("https://github.com/acme/widgets.git", "acme/widgets"),
        ("https://github.com/acme/widgets", "acme/widgets"),
        ("https://github.com/org/repo/", "org/repo"),
        # SSH-over-org form (some SSO setups):
        ("org-100@github.com:acme/widgets.git", "acme/widgets"),
    ],
)
def test_parse_github_slug_handles_common_url_forms(url: str, expected: str) -> None:
    assert parse_github_slug(url) == expected


@pytest.mark.parametrize("url", ["", "not a url", "ssh://", "https://github.com/"])
def test_parse_github_slug_returns_none_for_garbage(url: str) -> None:
    assert parse_github_slug(url) is None


# -------------------------------------------------------------------------
# _head_repo_for_branch integration-style test
# -------------------------------------------------------------------------


def test_head_repo_derives_slug_from_branch_upstream(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    """If a branch's upstream points at a distinct remote URL (fork
    scenario), `_head_repo_for_branch` returns that fork's slug."""
    repo_name, _ = stacker_repo
    service = StackerService(
        StackerDB(pm_env.stacker_db()),
        pm_env,
        pr_backend=RecordingPRBackend(),
    )
    slot = three_slots[0]
    tracked = service.init_new_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=slot.path,
            branch="feature-fork",
            parent=ParentLocator(repo_name=repo_name, branch="main"),
        )
    )

    # Config-only setup — stacker reads branch.X.remote/merge directly.
    stacker_git.git(
        slot.path,
        "remote",
        "add",
        "forkremote",
        "git@github.com:acme/widgets-dev.git",
    )
    stacker_git.git(slot.path, "config", "branch.feature-fork.remote", "forkremote")
    stacker_git.git(
        slot.path,
        "config",
        "branch.feature-fork.merge",
        "refs/heads/feature-fork",
    )

    assert head_repo_for_branch(service.ctx, tracked) == "acme/widgets-dev"


def test_head_repo_returns_none_when_no_upstream(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
) -> None:
    repo_name, _ = stacker_repo
    service = StackerService(
        StackerDB(pm_env.stacker_db()),
        pm_env,
        pr_backend=RecordingPRBackend(),
    )
    tracked = service.init_new_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=three_slots[0].path,
            branch="feature-no-upstream",
            parent=ParentLocator(repo_name=repo_name, branch="main"),
        )
    )
    assert head_repo_for_branch(service.ctx, tracked) is None
