from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.stacker import gh, git
from project_manager.stacker.db import StackerDB
from project_manager.stacker.pr.config import pr_config
from project_manager.stacker.service import StackerService

from .fakes import RecordingPRBackend


@pytest.fixture
def backend() -> RecordingPRBackend:
    return RecordingPRBackend(
        default_repo=gh.RepoInfo(name_with_owner="acme/widgets", owner="acme", name="widgets")
    )


@pytest.fixture
def service(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],  # noqa: ARG001 (creates repo on disk)
    backend: RecordingPRBackend,
) -> StackerService:
    return StackerService(StackerDB(pm_env.stacker_db()), pm_env, pr_backend=backend)


def test_default_mode_is_pr_pr(service: StackerService) -> None:
    cfg = pr_config(service.ctx, "demo")
    assert cfg.mode == "pr-pr"
    assert cfg.target_repo == "acme/widgets"


def test_set_same_repo_target_keeps_pr_pr_mode(service: StackerService) -> None:
    notices = service.set_config("demo", "pr.target-repo", "acme/widgets")
    assert notices == []
    assert pr_config(service.ctx, "demo").mode == "pr-pr"


def test_set_divergent_target_autoflips_to_repo_pr(service: StackerService) -> None:
    notices = service.set_config("demo", "pr.target-repo", "other/fork")
    assert any("repo-pr" in n for n in notices)
    cfg2 = pr_config(service.ctx, "demo")
    assert cfg2.mode == "repo-pr"
    assert cfg2.target_repo == "other/fork"


def test_cannot_set_pr_pr_when_target_differs(service: StackerService) -> None:
    service.set_config("demo", "pr.target-repo", "other/fork")
    with pytest.raises(git.GitError, match=r"pr\.mode=pr-pr"):
        service.set_config("demo", "pr.mode", "pr-pr")


def test_unset_target_allows_pr_pr(service: StackerService) -> None:
    service.set_config("demo", "pr.target-repo", "other/fork")
    service.unset_config("demo", "pr.target-repo")
    notices = service.set_config("demo", "pr.mode", "pr-pr")
    assert notices == []
    assert pr_config(service.ctx, "demo").mode == "pr-pr"


def test_unknown_key_rejected_on_set(service: StackerService) -> None:
    with pytest.raises(git.GitError, match="Unknown config key"):
        service.set_config("demo", "nope", "x")


def test_unknown_key_rejected_on_get(service: StackerService) -> None:
    with pytest.raises(git.GitError, match="Unknown config key"):
        service.get_config("demo", "nope")


def test_invalid_mode_value_rejected(service: StackerService) -> None:
    with pytest.raises(git.GitError, match=r"pr\.mode must be"):
        service.set_config("demo", "pr.mode", "normal")


def test_invalid_target_repo_format_rejected(service: StackerService) -> None:
    with pytest.raises(git.GitError, match="owner/repo"):
        service.set_config("demo", "pr.target-repo", "notaslug")


def test_corrupt_mode_value_surfaces_as_error(service: StackerService) -> None:
    # Bypass validation by writing directly to the DB.
    service.db.set_config("demo", "pr.mode", "bogus")
    with pytest.raises(git.GitError, match=r"Stored pr\.mode"):
        pr_config(service.ctx, "demo")
