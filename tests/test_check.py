import shutil

from project_manager import check
from project_manager.paths import Paths
from project_manager.pool.db import OWNER_STACKER_OPS, Owner, OwnerKind, PoolDB
from project_manager.project import add as add_mod
from project_manager.project import attach as attach_mod
from project_manager.project import db as project_db
from project_manager.project import detach as detach_mod
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import OperationState
from tests.helpers import git_in_slot, git_pool


def _mk_pool(paths: Paths, repo: str, uuids: list[str]) -> None:
    (paths.worktrees / repo).mkdir()
    for u in uuids:
        (paths.worktrees / repo / u).mkdir()


def _kinds(findings: list[check.Finding]) -> list[check.Kind]:
    return [f.kind for f in findings]


def _just(wts: list[str]) -> list[tuple[str, str]]:
    return [(w, w) for w in wts]


def test_healthy_active(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    add_mod.add(pm_env, "demo", _just(["foo"]))
    findings = check.check(pm_env)
    assert _kinds(findings) == [check.Kind.ACTIVE]


def test_detached_reported(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just(["foo"]))
    detach_mod.detach(pm_env, "demo", wts=None)
    findings = check.check(pm_env)
    assert _kinds(findings) == [check.Kind.DETACHED]
    assert check.fix(pm_env, findings) == 0


def test_broken_forward(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    add_mod.add(pm_env, "demo", _just(["foo"]))
    shutil.rmtree(pm_env.worktrees / "foo" / "a")
    findings = check.check(pm_env)
    assert check.Kind.BROKEN in _kinds(findings)
    check.fix(pm_env, findings)
    assert not (pm_env.projects / "demo" / "foo").exists()


def test_stale_when_pool_row_missing(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    add_mod.add(pm_env, "demo", _just(["foo"]))
    PoolDB(pm_env.pool_db()).release("foo", "a")
    findings = check.check(pm_env)
    assert check.Kind.STALE in _kinds(findings)
    check.fix(pm_env, findings)
    assert not (pm_env.projects / "demo" / "foo").exists()


def test_drift_is_informational(pm_env: Paths) -> None:
    slots = git_pool(pm_env, "foo", n=2)
    add_mod.add(pm_env, "demo", _just(["foo"]))
    detach_mod.detach(pm_env, "demo", wts=None)
    with project_db.readonly(pm_env.project_db("demo")) as conn:
        remembered = {w: u for w, _, u in project_db.list_wts(conn)}["foo"]
    other = next(s for s in slots if s.uuid != remembered)
    fwd = pm_env.projects / "demo" / "foo"
    PoolDB(pm_env.pool_db()).claim("foo", other.uuid, Owner(OwnerKind.PROJECT, "demo"))
    fwd.symlink_to(other.path)
    findings = check.check(pm_env)
    assert check.Kind.DRIFT in _kinds(findings)
    assert check.fix(pm_env, findings) == 0


def test_orphan_forward_no_db_row(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x"])
    add_mod.add(pm_env, "demo", _just(["foo"]))
    # stray forward with no matching db row
    fwd = pm_env.projects / "demo" / "bar"
    fwd.symlink_to(pm_env.worktrees / "bar" / "x")
    findings = check.check(pm_env)
    assert check.Kind.ORPHAN_FORWARD in _kinds(findings)
    check.fix(pm_env, findings)
    assert not fwd.exists()


def test_orphan_owner_points_at_nonmember_forward(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    add_mod.add(pm_env, "demo", _just(["foo"]))
    # manually remove the forward, leaving the pool row stranded
    (pm_env.projects / "demo" / "foo").unlink()
    findings = check.check(pm_env)
    assert check.Kind.ORPHAN_OWNER in _kinds(findings)
    check.fix(pm_env, findings)
    assert PoolDB(pm_env.pool_db()).get_owner("foo", "a") is None


def test_attach_after_detach_round_trips_clean(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just(["foo"]))
    detach_mod.detach(pm_env, "demo", wts=None)
    attach_mod.attach(pm_env, "demo", wts=None)
    findings = check.check(pm_env)
    assert _kinds(findings) == [check.Kind.ACTIVE]


def test_ops_owned_slot_is_classified(pm_env: Paths) -> None:
    """A stacker ops claim mid-cherry-pick reports OPS_OWNED and survives --fix.

    `CHERRY_PICK_HEAD` is the canonical "paused, resumable" signal — a
    `pm stacker continue` is on the user's roadmap, so we mustn't release.
    """
    [slot_obj] = git_pool(pm_env, "foo", n=1)
    pooldb = PoolDB(pm_env.pool_db())
    pooldb.claim(slot_obj.repo, slot_obj.uuid, OWNER_STACKER_OPS)
    # Mark the worktree as resumable: a real `CHERRY_PICK_HEAD` under the
    # slot's per-worktree git dir. Linked worktrees keep their git dir
    # under `.git/worktrees/<uuid>/` in the main repo, with a `gitdir:`
    # pointer in the slot's `.git` file — resolve through it.
    git_dir = slot_obj.path / ".git"
    if git_dir.is_file():
        gitdir_line = git_dir.read_text().splitlines()[0]
        real_git_dir = (slot_obj.path / gitdir_line.removeprefix("gitdir: ").strip()).resolve()
    else:
        real_git_dir = git_dir
    (real_git_dir / "CHERRY_PICK_HEAD").write_text("0" * 40 + "\n")

    findings = check.check(pm_env)
    assert _kinds(findings) == [check.Kind.OPS_OWNED]
    assert check.fix(pm_env, findings) == 0
    assert pooldb.get_owner(slot_obj.repo, slot_obj.uuid) == OWNER_STACKER_OPS


def test_stale_ops_slot_is_classified_and_fixed(pm_env: Paths) -> None:
    """A stacker ops claim on a clean worktree is a leaked claim.

    Common shape: a previous `pm stacker push` finished cleanly but
    didn't release (pre-fix behavior). `pm check --fix` must hand the
    slot back to the pool.
    """
    [slot_obj] = git_pool(pm_env, "foo", n=1)
    pooldb = PoolDB(pm_env.pool_db())
    pooldb.claim(slot_obj.repo, slot_obj.uuid, OWNER_STACKER_OPS)

    findings = check.check(pm_env)
    assert _kinds(findings) == [check.Kind.STALE_OPS]
    assert check.fix(pm_env, findings) == 1
    assert pooldb.get_owner(slot_obj.repo, slot_obj.uuid) is None


def test_clean_slot_for_paused_operation_is_not_reclaimed(pm_env: Paths) -> None:
    """The operation row can be resumable even without dirty Git state.

    A post-cherry-pick submodule failure has this shape: HEAD and the index
    are clean, but continue still needs the branch checkout named by the
    paused operation. ``pm check --fix`` must not detach it as stale.
    """
    [slot_obj] = git_pool(pm_env, "foo", n=1)
    git_in_slot(slot_obj.path, "checkout", "-b", "feature")
    pooldb = PoolDB(pm_env.pool_db())
    pooldb.claim(slot_obj.repo, slot_obj.uuid, OWNER_STACKER_OPS)
    StackerDB(pm_env.stacker_db()).put_operation(
        OperationState(
            repo_name="foo",
            op_type="local_sync",
            status="paused",
            branch="feature",
            parent_branch="main",
            error_message="submodule update failed",
        )
    )

    findings = check.check(pm_env)

    assert _kinds(findings) == [check.Kind.OPS_OWNED]
    assert check.fix(pm_env, findings) == 0
    assert pooldb.get_owner(slot_obj.repo, slot_obj.uuid) == OWNER_STACKER_OPS
