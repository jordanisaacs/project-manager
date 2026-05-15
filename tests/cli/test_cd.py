"""Tests for `pm cd` (see cli/cd.py)."""

import pytest

# Import for side-effect: registers every sub-app including `cd`.
import project_manager.cli  # noqa: F401
from project_manager.cli import main
from project_manager.paths import Paths
from project_manager.project import create as create_mod
from project_manager.project import detach as detach_mod
from tests.helpers import git_pool


def _just_repos(names: list[str]) -> list[tuple[str, str]]:
    return [(n, n) for n in names]


def test_cd_project_only_prints_project_dir(
    pm_env: Paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just_repos(["foo"]))
    assert main(["cd", "demo"]) == 0
    assert capsys.readouterr().out.strip() == str(pm_env.project("demo"))


def test_cd_print_flag_is_accepted(
    pm_env: Paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # `--print` is consumed by the shell wrapper, but must still parse
    # cleanly when the wrapper isn't sourced (or when scripts call it).
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just_repos(["foo"]))
    assert main(["cd", "--print", "demo"]) == 0
    assert capsys.readouterr().out.strip() == str(pm_env.project("demo"))


def test_cd_project_and_wt_prints_forward_symlink(
    pm_env: Paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", [("foo1", "foo")])
    assert main(["cd", "demo", "foo1"]) == 0
    assert capsys.readouterr().out.strip() == str(pm_env.forward("demo", "foo1"))


def test_cd_unknown_project_errors(
    pm_env: Paths,  # noqa: ARG001 (sets PM_CONFIG)
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["cd", "nope"]) == 2
    assert "project 'nope' does not exist" in capsys.readouterr().err


def test_cd_unknown_wt_errors(
    pm_env: Paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", [("foo1", "foo")])
    assert main(["cd", "demo", "missing"]) == 2
    assert "no worktree 'missing'" in capsys.readouterr().err


def test_cd_detached_wt_errors(
    pm_env: Paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", [("foo1", "foo")])
    detach_mod.detach(pm_env, "demo", ["foo1"])
    assert main(["cd", "demo", "foo1"]) == 2
    err = capsys.readouterr().err
    assert "detached" in err
    assert "pm project wt attach" in err


def test_cd_does_not_leak_path_on_error(
    pm_env: Paths,  # noqa: ARG001 (sets PM_CONFIG)
    capsys: pytest.CaptureFixture[str],
) -> None:
    # On error nothing must hit stdout — the shell wrapper would `cd ""`
    # otherwise, silently leaving the user in their current dir with no
    # explanation.
    main(["cd", "nope"])
    assert capsys.readouterr().out == ""
