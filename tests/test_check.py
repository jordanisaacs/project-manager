import shutil

from project_manager import check
from project_manager.paths import Paths
from project_manager.project import attach as attach_mod
from project_manager.project import detach as detach_mod
from project_manager.project import new as new_mod


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
    _mk_pool(pm_env, "foo", ["a"])
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


def test_stale_when_owner_missing(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    # manually remove .owner to simulate a crash
    (pm_env.worktrees / "foo" / "a" / ".owner").unlink()
    findings = check.check(pm_env)
    assert check.Kind.STALE in _kinds(findings)
    check.fix(pm_env, findings)
    assert not (pm_env.projects / "demo" / "foo").exists()


def test_drift_is_informational(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a", "b"])
    new_mod.new(pm_env, "demo", ["foo"])
    detach_mod.detach(pm_env, "demo", repos=None)
    # simulate drift: db says slot "a", but attach finds b taken, wait...
    # manually rewire demo's forward to point at slot b with a matching .owner
    fwd = pm_env.projects / "demo" / "foo"
    (pm_env.worktrees / "foo" / "b" / ".owner").symlink_to(fwd)
    fwd.symlink_to(pm_env.worktrees / "foo" / "b")
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
    # manually detach by just removing the forward (leaves .owner dangling)
    (pm_env.projects / "demo" / "foo").unlink()
    findings = check.check(pm_env)
    assert check.Kind.ORPHAN_OWNER in _kinds(findings)
    check.fix(pm_env, findings)
    assert not (pm_env.worktrees / "foo" / "a" / ".owner").exists()


def test_attach_after_detach_round_trips_clean(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    detach_mod.detach(pm_env, "demo", repos=None)
    attach_mod.attach(pm_env, "demo", repos=None)
    findings = check.check(pm_env)
    assert _kinds(findings) == [check.Kind.ACTIVE]
