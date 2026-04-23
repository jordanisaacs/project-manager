import pytest

from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.project import create as create_mod
from project_manager.project import current
from project_manager.project import detach as detach_mod
from tests.helpers import git_pool


def _mk_pool(paths: Paths, repo: str, uuids: list[str]) -> None:
    (paths.worktrees / repo).mkdir()
    for u in uuids:
        (paths.worktrees / repo / u).mkdir()


def _just(wts: list[str]) -> list[tuple[str, str]]:
    return [(w, w) for w in wts]


def _chdir(monkeypatch: pytest.MonkeyPatch, path) -> None:
    monkeypatch.chdir(path)
    monkeypatch.setenv("PWD", str(path))


def test_detects_from_forward_path(pm_env: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    create_mod.create(pm_env, "demo", _just(["foo"]))
    _chdir(monkeypatch, pm_env.projects / "demo" / "foo")
    assert current.detect_current_project(pm_env) == "demo"


def test_detects_from_physical_worktree_via_pool_db(
    pm_env: Paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    create_mod.create(pm_env, "demo", _just(["foo"]))
    _chdir(monkeypatch, pm_env.worktrees / "foo" / "a")
    assert current.detect_current_project(pm_env) == "demo"


def test_detach_while_inside_worktree_clears_detection(
    pm_env: Paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After detach the pool row is gone, so worktree-path detection can't recover
    the project name. Users in this corner case must pass the name explicitly.
    """
    slots = git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just(["foo"]))
    detach_mod.detach(pm_env, "demo", wts=None)
    _chdir(monkeypatch, slots[0].path)
    assert current.detect_current_project(pm_env) is None


def test_resolve_project_prefers_explicit(
    pm_env: Paths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    create_mod.create(pm_env, "demo", _just(["foo"]))
    _chdir(monkeypatch, pm_env.projects / "demo" / "foo")
    assert current.resolve_project(pm_env, "other") == "other"


def test_resolve_project_raises_when_nothing_to_detect(
    pm_env: Paths, tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _chdir(monkeypatch, tmp_path)
    with pytest.raises(ProjectError, match="no project specified"):
        current.resolve_project(pm_env, None)
