from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.stacker import git, selectors


def test_parse_selector_repo_branch() -> None:
    parsed = selectors.parse_selector("demo:feature-a")
    assert parsed.repo_query == "demo"
    assert parsed.branch == "feature-a"
    assert parsed.is_root is False


def test_parse_selector_bare_branch() -> None:
    parsed = selectors.parse_selector("feature-a")
    assert parsed.repo_query is None
    assert parsed.branch == "feature-a"


def test_resolve_repo_name_bare_name(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
) -> None:
    repo_name, _ = stacker_repo
    assert selectors.resolve_repo_name(pm_env, repo_name) == "demo"


def test_resolve_repo_name_unknown(pm_env: Paths) -> None:
    with pytest.raises(git.GitError):
        selectors.resolve_repo_name(pm_env, "nonexistent")


def test_resolve_target_requires_repo(pm_env: Paths) -> None:
    with pytest.raises(git.GitError):
        selectors.resolve_target(pm_env, "bare-branch")


def test_resolve_target_repo_colon_branch(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],  # noqa: ARG001
) -> None:
    target = selectors.resolve_target(pm_env, "demo:feature-a")
    assert target.repo_name == "demo"
    assert target.branch == "feature-a"


def test_resolve_parent_for_base_same_repo(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
) -> None:
    repo_name, _ = stacker_repo
    parent = selectors.resolve_parent_for_base(pm_env, repo_name, "main")
    assert parent.repo_name == "demo"
    assert parent.branch == "main"


def test_resolve_parent_for_base_rejects_cross_repo(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
) -> None:
    repo_name, _ = stacker_repo
    # Create a second repo.
    other = pm_env.repo("other")
    other.mkdir()
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
    with pytest.raises(git.GitError, match="same repo"):
        selectors.resolve_parent_for_base(pm_env, repo_name, "other:main")
