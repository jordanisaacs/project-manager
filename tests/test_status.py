import pytest

from project_manager import check as check_mod
from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.project import detach as detach_mod
from project_manager.project import new as new_mod
from project_manager.project import status as status_mod


def _mk_pool(paths: Paths, repo: str, uuids: list[str]) -> None:
    (paths.worktrees / repo).mkdir()
    for u in uuids:
        (paths.worktrees / repo / u).mkdir()


def test_status_healthy_project(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x"])
    new_mod.new(pm_env, "demo", ["foo", "bar"])
    rows = status_mod.status(pm_env, "demo")
    kinds = [r.finding.kind for r in rows]
    assert kinds == [check_mod.Kind.ACTIVE, check_mod.Kind.ACTIVE]
    by_repo = {r.repo: r for r in rows}
    assert by_repo["foo"].slot_uuid == "a"
    assert by_repo["bar"].slot_uuid == "x"


def test_status_includes_detached_row(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    detach_mod.detach(pm_env, "demo", repos=None)
    rows = status_mod.status(pm_env, "demo")
    assert len(rows) == 1
    assert rows[0].finding.kind == check_mod.Kind.DETACHED
    assert rows[0].repo == "foo"
    assert rows[0].slot_uuid == "a"


def test_status_raises_on_missing_project(pm_env: Paths) -> None:
    with pytest.raises(ProjectError, match="does not exist"):
        status_mod.status(pm_env, "ghost")


def test_status_is_scoped_to_one_project(pm_env: Paths) -> None:
    """Orphan owners pointing at a different project must not leak in."""
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x", "y"])
    new_mod.new(pm_env, "alpha", ["foo", "bar"])
    new_mod.new(pm_env, "beta", ["bar"])  # claims bar/y (x is taken by alpha's bar)
    # Inject an orphan owner on a fresh bar slot pointing at alpha's bogus forward.
    (pm_env.worktrees / "bar" / "z").mkdir()
    (pm_env.worktrees / "bar" / "z" / ".owner").symlink_to(
        pm_env.projects / "alpha" / "bogus",
    )
    alpha_rows = status_mod.status(pm_env, "alpha")
    beta_rows = status_mod.status(pm_env, "beta")
    # alpha sees the orphan owner (it points into alpha/...).
    assert any(r.finding.kind == check_mod.Kind.ORPHAN_OWNER for r in alpha_rows)
    # beta does not.
    assert all(r.finding.kind != check_mod.Kind.ORPHAN_OWNER for r in beta_rows)


def test_status_orphan_owner_row_has_no_repo_or_uuid(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    # Detach by just removing the forward (leaves .owner dangling → orphan owner).
    (pm_env.projects / "demo" / "foo").unlink()
    rows = status_mod.status(pm_env, "demo")
    orphans = [r for r in rows if r.finding.kind == check_mod.Kind.ORPHAN_OWNER]
    assert len(orphans) == 1
    assert orphans[0].repo is None
    assert orphans[0].slot_uuid is None
