import pytest

from project_manager.paths import Paths
from project_manager.pool.slot import PoolExhaustedError
from project_manager.project import delete as delete_mod
from project_manager.project import ls as ls_mod
from project_manager.project import new as new_mod
from project_manager.project import release as release_mod
from project_manager.project.errors import ProjectError


def _mk_pool(paths: Paths, repo: str, uuids: list[str]) -> None:
    (paths.worktrees / repo).mkdir()
    for u in uuids:
        (paths.worktrees / repo / u).mkdir()


def test_new_single_repo(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a", "b"])
    claimed = new_mod.new(pm_env, "demo", ["foo"])
    assert [repo for repo, _ in claimed] == ["foo"]
    assert (pm_env.projects / "demo" / "foo").is_symlink()
    assert (pm_env.worktrees / "foo" / "a" / ".owner").is_symlink()


def test_new_multi_repo(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x"])
    new_mod.new(pm_env, "demo", ["foo", "bar"])
    assert (pm_env.projects / "demo" / "foo").is_symlink()
    assert (pm_env.projects / "demo" / "bar").is_symlink()


def test_new_rolls_back_when_second_repo_exhausted(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", [])  # pool exists but no slots
    with pytest.raises(PoolExhaustedError):
        new_mod.new(pm_env, "demo", ["foo", "bar"])
    # foo's slot must be free again
    assert not (pm_env.worktrees / "foo" / "a" / ".owner").exists()
    # forward symlinks gone
    assert not (pm_env.projects / "demo").exists()


def test_new_fails_if_project_repo_link_exists(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    # second call to new for same project should fail (forward exists)
    _mk_pool(pm_env, "bar", ["x"])
    with pytest.raises(ProjectError):
        new_mod.new(pm_env, "demo", ["foo"])


def test_release_keeps_forward_symlinks(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    release_mod.release(pm_env, "demo")
    assert (pm_env.projects / "demo" / "foo").is_symlink()  # forward still here
    assert not (pm_env.worktrees / "foo" / "a" / ".owner").exists()  # .owner gone


def test_release_skips_stale_forward(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    release_mod.release(pm_env, "demo")
    # another project claims the same slot
    new_mod.new(pm_env, "other", ["foo"])
    # demo's forward still points at the slot but .owner now points at other's forward
    # releasing demo a second time must NOT remove other's .owner
    release_mod.release(pm_env, "demo")
    assert (pm_env.worktrees / "foo" / "a" / ".owner").is_symlink()


def test_delete_happy_path(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    delete_mod.delete(pm_env, "demo")
    assert not (pm_env.projects / "demo").exists()
    assert not (pm_env.worktrees / "foo" / "a" / ".owner").exists()


def test_delete_refuses_extra_files(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    (pm_env.projects / "demo" / "notes.txt").write_text("hi")
    with pytest.raises(ProjectError, match="non-pm entries"):
        delete_mod.delete(pm_env, "demo")
    assert (pm_env.projects / "demo").is_dir()
    assert (pm_env.worktrees / "foo" / "a" / ".owner").is_symlink()


def test_delete_refuses_non_pm_symlink(pm_env: Paths, tmp_path) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (pm_env.projects / "demo" / "stray").symlink_to(elsewhere)
    with pytest.raises(ProjectError):
        delete_mod.delete(pm_env, "demo")


def test_ls_status_active(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    rows = ls_mod.ls(pm_env)
    assert len(rows) == 1
    assert rows[0].status == "active"
    assert rows[0].project == "demo"
    assert rows[0].repo == "foo"


def test_ls_status_detached_after_release(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    release_mod.release(pm_env, "demo")
    rows = ls_mod.ls(pm_env)
    assert rows[0].status == "detached"


def test_ls_status_stale(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    release_mod.release(pm_env, "demo")
    new_mod.new(pm_env, "other", ["foo"])
    rows = ls_mod.ls(pm_env)
    rows_by_project = {r.project: r for r in rows}
    assert rows_by_project["demo"].status == "stale"
    assert rows_by_project["other"].status == "active"
