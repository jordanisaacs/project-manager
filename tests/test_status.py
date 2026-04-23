import pytest

from project_manager import check as check_mod
from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.pool.db import Owner, OwnerKind, PoolDB
from project_manager.project import create as create_mod
from project_manager.project import db as project_db
from project_manager.project import detach as detach_mod
from project_manager.project import status as status_mod
from tests.helpers import git_pool


def _mk_pool(paths: Paths, repo: str, uuids: list[str]) -> None:
    (paths.worktrees / repo).mkdir()
    for u in uuids:
        (paths.worktrees / repo / u).mkdir()


def _just(wts: list[str]) -> list[tuple[str, str]]:
    return [(w, w) for w in wts]


def test_status_healthy_project(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x"])
    create_mod.create(pm_env, "demo", _just(["foo", "bar"]))
    rows = status_mod.status(pm_env, "demo")
    kinds = [r.finding.kind for r in rows]
    assert kinds == [check_mod.Kind.ACTIVE, check_mod.Kind.ACTIVE]
    by_wt = {r.wt: r for r in rows}
    assert by_wt["foo"].slot_uuid == "a"
    assert by_wt["bar"].slot_uuid == "x"
    assert by_wt["foo"].repo == "foo"


def test_status_includes_detached_row(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just(["foo"]))
    with project_db.readonly(pm_env.project_db("demo")) as conn:
        uuid = {w: u for w, _, u in project_db.list_wts(conn)}["foo"]
    detach_mod.detach(pm_env, "demo", wts=None)
    rows = status_mod.status(pm_env, "demo")
    assert len(rows) == 1
    assert rows[0].finding.kind == check_mod.Kind.DETACHED
    assert rows[0].wt == "foo"
    assert rows[0].repo == "foo"
    assert rows[0].slot_uuid == uuid


def test_status_raises_on_missing_project(pm_env: Paths) -> None:
    with pytest.raises(ProjectError, match="does not exist"):
        status_mod.status(pm_env, "ghost")


def test_status_is_scoped_to_one_project(pm_env: Paths) -> None:
    """Orphan owners for a different project must not leak in."""
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x", "y"])
    create_mod.create(pm_env, "alpha", _just(["foo", "bar"]))
    create_mod.create(pm_env, "beta", _just(["bar"]))  # claims bar/y (x is taken by alpha's bar)
    # Inject an orphan owner: a fresh bar slot claimed by alpha in the pool db
    # with no matching forward.
    (pm_env.worktrees / "bar" / "z").mkdir()
    PoolDB(pm_env.pool_db()).claim("bar", "z", Owner(OwnerKind.PROJECT, "alpha"))
    alpha_rows = status_mod.status(pm_env, "alpha")
    beta_rows = status_mod.status(pm_env, "beta")
    assert any(r.finding.kind == check_mod.Kind.ORPHAN_OWNER for r in alpha_rows)
    assert all(r.finding.kind != check_mod.Kind.ORPHAN_OWNER for r in beta_rows)


def test_status_orphan_owner_row_has_no_wt_or_uuid(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    create_mod.create(pm_env, "demo", _just(["foo"]))
    # Detach by just removing the forward (leaves pool row dangling → orphan owner).
    (pm_env.projects / "demo" / "foo").unlink()
    rows = status_mod.status(pm_env, "demo")
    orphans = [r for r in rows if r.finding.kind == check_mod.Kind.ORPHAN_OWNER]
    assert len(orphans) == 1
    assert orphans[0].wt is None
    assert orphans[0].slot_uuid is None
    # repo is knowable from the pool key even without a forward symlink
    assert orphans[0].repo == "foo"
