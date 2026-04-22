import shutil
import sqlite3

import pytest

from project_manager.paths import Paths
from project_manager.pool.slot import PoolExhaustedError
from project_manager.project import attach as attach_mod
from project_manager.project import db
from project_manager.project import delete as delete_mod
from project_manager.project import detach as detach_mod
from project_manager.project import ls as ls_mod
from project_manager.project import new as new_mod
from project_manager.project.errors import ProjectError


def _mk_pool(paths: Paths, repo: str, uuids: list[str]) -> None:
    (paths.worktrees / repo).mkdir()
    for u in uuids:
        (paths.worktrees / repo / u).mkdir()


def _db_rows(paths: Paths, project: str) -> list[tuple[str, str]]:
    with db.readonly(paths.project_db(project)) as conn:
        return db.list_repos(conn)


# --- new ---


def test_new_single_repo_creates_db_and_symlinks(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    assert (pm_env.projects / "demo" / ".pm.db").is_file()
    assert (pm_env.projects / "demo" / "foo").is_symlink()
    assert (pm_env.worktrees / "foo" / "a" / ".owner").is_symlink()
    assert _db_rows(pm_env, "demo") == [("foo", "a")]


def test_new_multi_repo(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x"])
    new_mod.new(pm_env, "demo", ["foo", "bar"])
    assert sorted(_db_rows(pm_env, "demo")) == [("bar", "x"), ("foo", "a")]


def test_new_rolls_back_when_second_repo_exhausted(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", [])
    with pytest.raises(PoolExhaustedError):
        new_mod.new(pm_env, "demo", ["foo", "bar"])
    # filesystem rolled back
    assert not (pm_env.worktrees / "foo" / "a" / ".owner").exists()
    assert not (pm_env.projects / "demo").exists()


def test_new_fails_if_project_already_has_repo(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a", "b"])
    new_mod.new(pm_env, "demo", ["foo"])
    with pytest.raises(ProjectError, match="already has"):
        new_mod.new(pm_env, "demo", ["foo"])


# --- detach ---


def test_detach_unlinks_forward_and_owner_but_keeps_db(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    detach_mod.detach(pm_env, "demo", repos=None)
    assert not (pm_env.projects / "demo" / "foo").exists()
    assert not (pm_env.worktrees / "foo" / "a" / ".owner").exists()
    assert (pm_env.projects / "demo" / ".pm.db").is_file()
    assert _db_rows(pm_env, "demo") == [("foo", "a")]


def test_detach_per_repo(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x"])
    new_mod.new(pm_env, "demo", ["foo", "bar"])
    detach_mod.detach(pm_env, "demo", repos=["foo"])
    assert not (pm_env.projects / "demo" / "foo").exists()
    assert (pm_env.projects / "demo" / "bar").is_symlink()


def test_detach_refuses_unknown_repo(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    with pytest.raises(ProjectError, match="no such repo"):
        detach_mod.detach(pm_env, "demo", repos=["nope"])


# --- attach ---


def test_attach_reclaims_same_slot_when_free(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a", "b"])
    new_mod.new(pm_env, "demo", ["foo"])
    rows_before = _db_rows(pm_env, "demo")
    detach_mod.detach(pm_env, "demo", repos=None)
    attach_mod.attach(pm_env, "demo", repos=None)
    assert _db_rows(pm_env, "demo") == rows_before  # same uuid
    forward_target = (pm_env.projects / "demo" / "foo").readlink()
    assert forward_target.name == rows_before[0][1]


def test_attach_falls_back_and_updates_row(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a", "b"])
    new_mod.new(pm_env, "demo", ["foo"])
    remembered = _db_rows(pm_env, "demo")[0][1]
    detach_mod.detach(pm_env, "demo", repos=None)
    # steal demo's remembered slot
    new_mod.new(pm_env, "other", ["foo"])
    # demo's attach should fall back
    attach_mod.attach(pm_env, "demo", repos=None)
    new_uuid = _db_rows(pm_env, "demo")[0][1]
    assert new_uuid != remembered
    assert (pm_env.projects / "demo" / "foo").readlink().name == new_uuid


def test_attach_errors_when_pool_exhausted_for_fallback(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    detach_mod.detach(pm_env, "demo", repos=None)
    # the one slot is now stolen by another project, no free slots
    new_mod.new(pm_env, "other", ["foo"])
    with pytest.raises(PoolExhaustedError):
        attach_mod.attach(pm_env, "demo", repos=None)


def test_attach_partial_and_all(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x"])
    new_mod.new(pm_env, "demo", ["foo", "bar"])
    detach_mod.detach(pm_env, "demo", repos=None)
    attach_mod.attach(pm_env, "demo", repos=["foo"])
    assert (pm_env.projects / "demo" / "foo").is_symlink()
    assert not (pm_env.projects / "demo" / "bar").is_symlink()
    attach_mod.attach(pm_env, "demo", repos=None)
    assert (pm_env.projects / "demo" / "bar").is_symlink()


def test_attach_idempotent_when_already_attached(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    claimed = attach_mod.attach(pm_env, "demo", repos=None)
    assert claimed == []  # nothing newly claimed


def test_attach_errors_on_nonexistent_project(pm_env: Paths) -> None:
    with pytest.raises(ProjectError, match="does not exist"):
        attach_mod.attach(pm_env, "ghost", repos=None)


# --- delete ---


def test_delete_whole_removes_db_and_dir(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    delete_mod.delete(pm_env, "demo", repos=None)
    assert not (pm_env.projects / "demo").exists()
    assert not (pm_env.worktrees / "foo" / "a" / ".owner").exists()


def test_delete_per_repo_implicit_detach(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x"])
    new_mod.new(pm_env, "demo", ["foo", "bar"])
    delete_mod.delete(pm_env, "demo", repos=["foo"])
    assert not (pm_env.projects / "demo" / "foo").exists()
    assert not (pm_env.worktrees / "foo" / "a" / ".owner").exists()
    assert (pm_env.projects / "demo" / "bar").is_symlink()
    assert _db_rows(pm_env, "demo") == [("bar", "x")]


def test_delete_per_repo_when_already_detached(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    detach_mod.detach(pm_env, "demo", repos=None)
    delete_mod.delete(pm_env, "demo", repos=["foo"])
    assert _db_rows(pm_env, "demo") == []


def test_delete_whole_refuses_extra_files(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    (pm_env.projects / "demo" / "notes.txt").write_text("hi")
    with pytest.raises(ProjectError, match="non-pm entries"):
        delete_mod.delete(pm_env, "demo", repos=None)
    # project still intact
    assert (pm_env.projects / "demo" / ".pm.db").is_file()


def test_delete_refuses_unknown_repo(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    with pytest.raises(ProjectError, match="no such repo"):
        delete_mod.delete(pm_env, "demo", repos=["nope"])


# --- ls ---


def test_ls_reports_mixed_states(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x"])
    new_mod.new(pm_env, "demo", ["foo", "bar"])
    detach_mod.detach(pm_env, "demo", repos=["bar"])
    rows = ls_mod.ls(pm_env)
    by_repo = {r.repo: r for r in rows}
    assert by_repo["foo"].status == "attached"
    assert by_repo["bar"].status == "detached"


def test_ls_drift(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a", "b"])
    new_mod.new(pm_env, "demo", ["foo"])
    # detach, swap forward to a different (claimed) slot manually to simulate drift
    detach_mod.detach(pm_env, "demo", repos=None)
    # manually claim slot b and forward-link to b instead of a (while db remembers a)
    other_forward = pm_env.projects / "demo" / "foo"
    (pm_env.worktrees / "foo" / "b" / ".owner").symlink_to(other_forward)
    other_forward.symlink_to(pm_env.worktrees / "foo" / "b")
    rows = ls_mod.ls(pm_env)
    assert rows[0].status == "drift"


# --- migration snippet ---


def test_migration_snippet(pm_env: Paths) -> None:
    # v1-style state: forward symlinks, owner markers, but no .pm.db
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x"])
    (pm_env.projects / "demo").mkdir()
    (pm_env.projects / "demo" / "foo").symlink_to(pm_env.worktrees / "foo" / "a")
    (pm_env.projects / "demo" / "bar").symlink_to(pm_env.worktrees / "bar" / "x")
    (pm_env.worktrees / "foo" / "a" / ".owner").symlink_to(pm_env.projects / "demo" / "foo")
    (pm_env.worktrees / "bar" / "x" / ".owner").symlink_to(pm_env.projects / "demo" / "bar")

    # run the migration snippet from the plan
    for proj in pm_env.projects.iterdir():
        if not proj.is_dir() or (proj / ".pm.db").exists():
            continue
        conn = sqlite3.connect(proj / ".pm.db")
        conn.execute(
            "CREATE TABLE repos (name TEXT PRIMARY KEY, slot_uuid TEXT NOT NULL)"
        )
        for entry in proj.iterdir():
            if entry.is_symlink():
                conn.execute(
                    "INSERT INTO repos (name, slot_uuid) VALUES (?, ?)",
                    (entry.name, entry.readlink().name),
                )
        conn.commit()
        conn.close()

    assert sorted(_db_rows(pm_env, "demo")) == [("bar", "x"), ("foo", "a")]
    # pm sees this project fully
    rows = ls_mod.ls(pm_env)
    assert {(r.repo, r.status) for r in rows} == {("foo", "attached"), ("bar", "attached")}


# silences unused-import warning if test layout changes
_ = shutil
