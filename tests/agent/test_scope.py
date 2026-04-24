from pathlib import Path

from project_manager.agent import scope
from project_manager.paths import Paths
from project_manager.project import create as create_mod
from tests.helpers import git_pool


def _just(wts: list[str]) -> list[tuple[str, str]]:
    return [(w, w) for w in wts]


def test_owned_paths_includes_project_and_worktree_targets(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just(["foo"]))
    project_dir = pm_env.project("demo")
    symlink = pm_env.forward("demo", "foo")
    real_slot = symlink.resolve()
    owned = scope.owned_paths(pm_env, "demo")
    # Project dir (and its resolve), symlink path, and the real worktree
    # all need to be in the owned set so a session cwd recorded as any
    # of them matches this project.
    assert project_dir in owned
    assert symlink in owned
    assert real_slot in owned


def test_owned_paths_handles_broken_symlink_gracefully(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just(["foo"]))
    # Point the forward symlink at a non-existent path — simulates a
    # broken pool slot. owned_paths must not raise.
    symlink = pm_env.forward("demo", "foo")
    target = Path("/nonexistent/slot/path")
    symlink.unlink()
    symlink.symlink_to(target)
    owned = scope.owned_paths(pm_env, "demo")
    assert symlink in owned
