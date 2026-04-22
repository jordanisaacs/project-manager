import shutil

from project_manager import check
from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.project import new as new_mod
from project_manager.project import release as release_mod


def _mk_pool(paths: Paths, repo: str, uuids: list[str]) -> None:
    (paths.worktrees / repo).mkdir()
    for u in uuids:
        (paths.worktrees / repo / u).mkdir()


def test_healthy_shows_active(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    findings = check.check(pm_env)
    kinds = [f.kind for f in findings]
    assert check.Kind.ACTIVE in kinds
    assert check.Kind.ORPHAN not in kinds
    assert check.Kind.BROKEN not in kinds


def test_orphan_detected_when_forward_rmd(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    (pm_env.projects / "demo" / "foo").unlink()
    findings = check.check(pm_env)
    orphans = [f for f in findings if f.kind == check.Kind.ORPHAN]
    assert len(orphans) == 1


def test_fix_orphan(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    (pm_env.projects / "demo" / "foo").unlink()
    applied = check.fix(pm_env, check.check(pm_env))
    assert applied == 1
    assert not (pm_env.worktrees / "foo" / "a" / ".owner").exists()
    assert check.check(pm_env) == []


def test_detached_after_release(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    release_mod.release(pm_env, "demo")
    findings = check.check(pm_env)
    assert any(f.kind == check.Kind.DETACHED for f in findings)
    # fix should not touch detached
    applied = check.fix(pm_env, findings)
    assert applied == 0
    assert (pm_env.projects / "demo" / "foo").is_symlink()


def test_stale_detected(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    release_mod.release(pm_env, "demo")
    new_mod.new(pm_env, "other", ["foo"])
    findings = check.check(pm_env)
    stale = [f for f in findings if f.kind == check.Kind.STALE]
    assert len(stale) == 1


def test_broken_forward(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    # delete the slot entirely, leaving forward dangling
    shutil.rmtree(pm_env.worktrees / "foo" / "a")
    findings = check.check(pm_env)
    broken = [f for f in findings if f.kind == check.Kind.BROKEN]
    assert len(broken) == 1
    applied = check.fix(pm_env, findings)
    assert applied == 1
    assert not (pm_env.projects / "demo" / "foo").exists()


def test_crash_mid_claim_orphan(pm_env: Paths) -> None:
    """Simulate a crash after .owner created but before forward symlink."""
    _mk_pool(pm_env, "foo", ["a"])
    (pm_env.projects / "demo").mkdir()
    slot = slot_mod.list_slots(pm_env, "foo")[0]
    slot_mod.claim(slot, pm_env.forward("demo", "foo"))
    # crash here — no forward symlink

    findings = check.check(pm_env)
    orphans = [f for f in findings if f.kind == check.Kind.ORPHAN]
    assert len(orphans) == 1
    check.fix(pm_env, findings)
    assert not slot.owner_path.exists()
