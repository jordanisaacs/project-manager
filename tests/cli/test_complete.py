"""Smoke tests for `pm __complete` verbs (see cli/_complete.py)."""

from pathlib import Path

import pytest

# Import for side-effect: registers every sub-app including __complete.
import project_manager.cli  # noqa: F401
from project_manager.cli import main
from project_manager.cli._complete import projects, repos, worktrees
from project_manager.paths import Paths
from project_manager.project import create as create_mod
from tests.helpers import git_pool


def _just_repos(names: list[str]) -> list[tuple[str, str]]:
    return [(n, n) for n in names]


def test_projects_lists_created_projects(
    pm_env: Paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just_repos(["foo"]))
    assert projects() == 0
    assert capsys.readouterr().out == "demo\n"


def test_projects_empty_on_fresh_env(
    pm_env: Paths,  # noqa: ARG001 (sets PM_CONFIG)
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert projects() == 0
    assert capsys.readouterr().out == ""


def test_repos_lists_pool_repos(
    pm_env: Paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git_pool(pm_env, "foo", n=1)
    git_pool(pm_env, "bar", n=1)
    assert repos() == 0
    out = capsys.readouterr().out.splitlines()
    assert set(out) == {"foo", "bar"}


def test_repos_empty_on_fresh_env(
    pm_env: Paths,  # noqa: ARG001 (sets PM_CONFIG)
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert repos() == 0
    assert capsys.readouterr().out == ""


def test_worktrees_lists_project_wts(
    pm_env: Paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git_pool(pm_env, "foo", n=2)
    git_pool(pm_env, "bar", n=1)
    # Two worktrees in one project.
    create_mod.create(pm_env, "demo", [("foo1", "foo"), ("foo2", "foo")])
    # --project explicit.
    assert worktrees(project="demo") == 0
    out = capsys.readouterr().out.splitlines()
    assert set(out) == {"foo1", "foo2"}


def test_worktrees_unknown_project_silent(
    pm_env: Paths,  # noqa: ARG001 (sets PM_CONFIG)
    capsys: pytest.CaptureFixture[str],
) -> None:
    # require_project_db raises ProjectError; verb must swallow it.
    assert worktrees(project="nonexistent") == 0
    assert capsys.readouterr().out == ""


def test_worktrees_filter_by_attach_state(
    pm_env: Paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """state=attached/unattached filter correctly."""
    from project_manager.project import detach as detach_mod

    git_pool(pm_env, "foo", n=2)
    create_mod.create(pm_env, "demo", [("foo1", "foo"), ("foo2", "foo")])
    # Detach foo1 → it should appear in "unattached", not in "attached".
    detach_mod.detach(pm_env, "demo", ["foo1"])

    # attached → foo2 only
    assert worktrees(project="demo", state="attached") == 0
    assert capsys.readouterr().out.splitlines() == ["foo2"]

    # unattached → foo1 only
    assert worktrees(project="demo", state="unattached") == 0
    assert capsys.readouterr().out.splitlines() == ["foo1"]

    # all → both
    assert worktrees(project="demo", state="all") == 0
    assert set(capsys.readouterr().out.splitlines()) == {"foo1", "foo2"}


def test_worktrees_no_project_outside_cwd(
    pm_env: Paths,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No project, not inside a pm project tree: current.resolve_project errors.
    monkeypatch.chdir(pm_env.projects.parent)
    assert worktrees() == 0
    assert capsys.readouterr().out == ""


def test_verbs_swallow_broken_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pointing PM_CONFIG at a bogus file must not raise — completion should
    # stay silent and exit 0 regardless of config health.
    missing = tmp_path / "does-not-exist.toml"
    monkeypatch.setenv("PM_CONFIG", str(missing))
    assert projects() == 0
    assert repos() == 0
    assert worktrees() == 0
    assert capsys.readouterr().out == ""


def test_hidden_from_root_help(capsys: pytest.CaptureFixture[str]) -> None:
    # `pm --help` should not advertise `__complete`.
    # main() uses result_action="return_value", so --help prints + returns.
    main(["--help"])
    out = capsys.readouterr().out
    assert "__complete" not in out
    # Sanity: the visible commands do appear.
    assert "stacker" in out


def test_complete_app_help_does_not_crash() -> None:
    # Hidden sub-app still responds to --help without raising — keeps shell
    # completion from breaking if a user ever types `pm __complete --help`.
    main(["__complete", "--help"])
