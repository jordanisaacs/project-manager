import shutil

from project_manager import check
from project_manager.paths import Paths
from project_manager.pool.db import OWNER_STACKER_OPS, Owner, OwnerKind, PoolDB
from project_manager.project import attach as attach_mod
from project_manager.project import db as project_db
from project_manager.project import detach as detach_mod
from project_manager.project import new as new_mod
from tests.helpers import git_pool


def _mk_pool(paths: Paths, repo: str, uuids: list[str]) -> None:
    (paths.worktrees / repo).mkdir()
    for u in uuids:
        (paths.worktrees / repo / u).mkdir()


def _kinds(findings: list[check.Finding]) -> list[check.Kind]:
    return [f.kind for f in findings]


def test_healthy_active(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    findings = check.check(pm_env)
    assert _kinds(findings) == [check.Kind.ACTIVE]


def test_detached_reported(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    detach_mod.detach(pm_env, "demo", repos=None)
    findings = check.check(pm_env)
    assert _kinds(findings) == [check.Kind.DETACHED]
    # fix does not touch detached rows
    assert check.fix(pm_env, findings) == 0


def test_broken_forward(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    shutil.rmtree(pm_env.worktrees / "foo" / "a")
    findings = check.check(pm_env)
    assert check.Kind.BROKEN in _kinds(findings)
    check.fix(pm_env, findings)
    assert not (pm_env.projects / "demo" / "foo").exists()


def test_stale_when_pool_row_missing(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    # Manually delete the pool-db row to simulate a crashed claim.
    PoolDB(pm_env.pool_db()).release("foo", "a")
    findings = check.check(pm_env)
    assert check.Kind.STALE in _kinds(findings)
    check.fix(pm_env, findings)
    assert not (pm_env.projects / "demo" / "foo").exists()


def test_drift_is_informational(pm_env: Paths) -> None:
    slots = git_pool(pm_env, "foo", n=2)
    new_mod.new(pm_env, "demo", ["foo"])
    detach_mod.detach(pm_env, "demo", repos=None)
    # Simulate drift: forward and pool row both point at the OTHER slot, but the
    # project db still remembers the original.
    with project_db.readonly(pm_env.project_db("demo")) as conn:
        remembered = dict(project_db.list_repos(conn))["foo"]
    other = next(s for s in slots if s.uuid != remembered)
    fwd = pm_env.projects / "demo" / "foo"
    PoolDB(pm_env.pool_db()).claim("foo", other.uuid, Owner(OwnerKind.PROJECT, "demo"))
    fwd.symlink_to(other.path)
    findings = check.check(pm_env)
    assert check.Kind.DRIFT in _kinds(findings)
    # fix should be a no-op for drift
    assert check.fix(pm_env, findings) == 0


def test_orphan_forward_no_db_row(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x"])
    new_mod.new(pm_env, "demo", ["foo"])
    # stray forward with no matching db row
    fwd = pm_env.projects / "demo" / "bar"
    fwd.symlink_to(pm_env.worktrees / "bar" / "x")
    findings = check.check(pm_env)
    assert check.Kind.ORPHAN_FORWARD in _kinds(findings)
    check.fix(pm_env, findings)
    assert not fwd.exists()


def test_orphan_owner_points_at_nonmember_forward(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    # manually remove the forward, leaving the pool row stranded
    (pm_env.projects / "demo" / "foo").unlink()
    findings = check.check(pm_env)
    assert check.Kind.ORPHAN_OWNER in _kinds(findings)
    check.fix(pm_env, findings)
    assert PoolDB(pm_env.pool_db()).get_owner("foo", "a") is None


def test_attach_after_detach_round_trips_clean(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    detach_mod.detach(pm_env, "demo", repos=None)
    attach_mod.attach(pm_env, "demo", repos=None)
    findings = check.check(pm_env)
    assert _kinds(findings) == [check.Kind.ACTIVE]


def test_ops_owned_slot_is_classified(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    pooldb = PoolDB(pm_env.pool_db())
    pooldb.claim("foo", "a", OWNER_STACKER_OPS)

    findings = check.check(pm_env)
    assert _kinds(findings) == [check.Kind.OPS_OWNED]
    # fix() does NOT touch OPS_OWNED slots — they're intentional.
    assert check.fix(pm_env, findings) == 0
    assert pooldb.get_owner("foo", "a") == OWNER_STACKER_OPS
