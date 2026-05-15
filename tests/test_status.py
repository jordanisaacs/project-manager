from pathlib import Path

import pytest

from project_manager import check as check_mod
from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.pool.db import Owner, OwnerKind, PoolDB
from project_manager.project import create as create_mod
from project_manager.project import db as project_db
from project_manager.project import detach as detach_mod
from project_manager.project import status as status_mod
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import PRState, TrackedBranch
from tests.helpers import git_in_slot, git_pool, head_ref


def _mk_pool(paths: Paths, repo: str, uuids: list[str]) -> None:
    (paths.worktrees / repo).mkdir()
    for u in uuids:
        (paths.worktrees / repo / u).mkdir()


def _just(wts: list[str]) -> list[tuple[str, str]]:
    return [(w, w) for w in wts]


def _checkout_branch(forward: Path, branch: str) -> None:
    """Pool slots are minted detached — put the slot on a named branch."""
    slot = forward.resolve()
    git_in_slot(slot, "checkout", "-B", branch)


def _tracked(repo: str, branch: str, parent: str) -> TrackedBranch:
    return TrackedBranch(
        repo_name=repo,
        branch=branch,
        parent_repo_name=repo,
        parent_branch=parent,
        managed_base_commit="0" * 40,
        last_synced_parent_commit=None,
        last_clean_head=None,
    )


def test_status_healthy_project(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x"])
    create_mod.create(pm_env, "demo", _just(["foo", "bar"]))
    ps = status_mod.status(pm_env, "demo")
    kinds = [r.finding.kind for r in ps.worktrees]
    assert kinds == [check_mod.Kind.ACTIVE, check_mod.Kind.ACTIVE]
    by_wt = {r.wt: r for r in ps.worktrees}
    assert by_wt["foo"].repo == "foo"
    assert by_wt["bar"].repo == "bar"
    # Non-git stub pool dirs — no branch resolvable.
    assert all(r.branch is None for r in ps.worktrees)
    assert ps.prs == []


def test_status_resolves_branch_for_active_row(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just(["foo"]))
    forward = pm_env.projects / "demo" / "foo"
    _checkout_branch(forward, "feat")
    ps = status_mod.status(pm_env, "demo")
    assert len(ps.worktrees) == 1
    row = ps.worktrees[0]
    assert row.finding.kind == check_mod.Kind.ACTIVE
    assert row.branch == "feat"
    assert row.pr is None


def test_status_includes_detached_row_with_persisted_branch(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just(["foo"]))
    forward = pm_env.projects / "demo" / "foo"
    _checkout_branch(forward, "feat")
    persisted = head_ref(forward.resolve())
    assert persisted == "feat"
    detach_mod.detach(pm_env, "demo", wts=None)
    ps = status_mod.status(pm_env, "demo")
    assert len(ps.worktrees) == 1
    row = ps.worktrees[0]
    assert row.finding.kind == check_mod.Kind.DETACHED
    assert row.wt == "foo"
    assert row.repo == "foo"
    # `worktrees.branch` is restored on re-attach; detached row surfaces it here.
    assert row.branch == persisted


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
    alpha = status_mod.status(pm_env, "alpha")
    beta = status_mod.status(pm_env, "beta")
    assert any(r.finding.kind == check_mod.Kind.ORPHAN_OWNER for r in alpha.worktrees)
    assert all(r.finding.kind != check_mod.Kind.ORPHAN_OWNER for r in beta.worktrees)


def test_status_orphan_owner_row_has_no_wt(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    create_mod.create(pm_env, "demo", _just(["foo"]))
    # Detach by just removing the forward (leaves pool row dangling → orphan owner).
    (pm_env.projects / "demo" / "foo").unlink()
    ps = status_mod.status(pm_env, "demo")
    orphans = [r for r in ps.worktrees if r.finding.kind == check_mod.Kind.ORPHAN_OWNER]
    assert len(orphans) == 1
    assert orphans[0].wt is None
    assert orphans[0].branch is None
    # repo is knowable from the pool key even without a forward symlink
    assert orphans[0].repo == "foo"


def test_status_populates_prs_when_stacker_db_has_match(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just(["foo"]))
    forward = pm_env.projects / "demo" / "foo"
    _checkout_branch(forward, "feat")
    pr = PRState(
        repo_name="foo",
        branch="feat",
        pr_url="https://github.com/acme/foo/pull/42",
        pr_number=42,
        state="OPEN",
        is_approved=True,
    )
    StackerDB(pm_env.stacker_db()).upsert_pr_state(pr)
    ps = status_mod.status(pm_env, "demo")
    assert len(ps.worktrees) == 1
    assert ps.worktrees[0].pr is not None
    assert ps.worktrees[0].pr.pr_url == pr.pr_url
    assert len(ps.prs) == 1
    assert ps.prs[0].wt == "foo"
    assert ps.prs[0].branch == "feat"
    assert ps.prs[0].pr.is_approved is True


def test_status_pr_list_empty_without_match(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just(["foo"]))
    pr = PRState(
        repo_name="foo",
        branch="some-other-branch",
        pr_url="https://github.com/acme/foo/pull/1",
        pr_number=1,
        state="OPEN",
    )
    StackerDB(pm_env.stacker_db()).upsert_pr_state(pr)
    ps = status_mod.status(pm_env, "demo")
    assert ps.prs == []
    assert all(r.pr is None for r in ps.worktrees)


def test_status_worktree_sections_group_by_repo(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x"])
    create_mod.create(pm_env, "demo", [("foo-wt", "foo"), ("bar-wt", "bar")])
    ps = status_mod.status(pm_env, "demo")
    sections = status_mod.worktree_sections(ps.worktrees)
    titles = [s.title for s in sections]
    assert titles == sorted(titles)
    assert set(titles) == {"foo", "bar"}
    for section in sections:
        rows = list(section.rows)
        assert all(r.repo == section.title for r in rows)


def test_worktree_row_json_shape_drops_slot_uuid(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    create_mod.create(pm_env, "demo", _just(["foo"]))
    ps = status_mod.status(pm_env, "demo")
    row = ps.worktrees[0]
    data = row.__pm_json__()
    assert "slot_uuid" not in data
    assert set(data) >= {"wt", "repo", "branch", "pr", "tracked", "kind", "detail"}
    # `wt` / `slot_uuid` replacement confirmation — old callers that
    # relied on slot_uuid need to pivot to the project db or discovery.
    conn_path = pm_env.project_db("demo")
    with project_db.readonly(conn_path) as conn:
        recorded = {w: u for w, _, u in project_db.list_wts(conn)}
    assert recorded["foo"] == "a"


def test_worktree_row_tracked_flag_follows_stacker_db(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just(["foo"]))
    forward = pm_env.projects / "demo" / "foo"
    _checkout_branch(forward, "feat")
    # Initially not tracked
    ps = status_mod.status(pm_env, "demo")
    assert ps.worktrees[0].tracked is False
    assert ps.stacker == []
    # After upsert: tracked=True AND stacker section populated for the repo
    StackerDB(pm_env.stacker_db()).upsert_branch(_tracked("foo", "feat", "main"))
    ps = status_mod.status(pm_env, "demo")
    assert ps.worktrees[0].tracked is True
    assert len(ps.stacker) == 1
    assert ps.stacker[0].repo == "foo"
    # The existing stacker tree renderer decorates the branch name with
    # rich markup; assert on the branch text rather than raw markup.
    assert "feat" in ps.stacker[0].info


def test_stacker_section_unions_arms_across_worktrees_same_repo(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=2)
    create_mod.create(pm_env, "demo", [("w1", "foo"), ("w2", "foo")])
    _checkout_branch(pm_env.projects / "demo" / "w1", "arm-a")
    _checkout_branch(pm_env.projects / "demo" / "w2", "arm-b")
    db = StackerDB(pm_env.stacker_db())
    db.upsert_branch(_tracked("foo", "arm-a", "main"))
    db.upsert_branch(_tracked("foo", "arm-b", "main"))
    ps = status_mod.status(pm_env, "demo")
    assert len(ps.stacker) == 1  # one repo → one row
    info = ps.stacker[0].info
    # Two distinct arms, so both branch names appear in the rendered info
    assert "arm-a" in info
    assert "arm-b" in info


def test_worktree_columns_do_not_expose_path() -> None:
    titles = [c.title for c in status_mod.WORKTREE_COLUMNS]
    assert "Path" not in titles
    assert "Stacker" in titles


def test_stacker_tree_marks_branches_with_wt_name_not_current(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    # Use a worktree name distinct from both branch and repo so the
    # `(wt)` label is unambiguously the worktree name, not a fallback.
    create_mod.create(pm_env, "demo", [("slot-one", "foo")])
    _checkout_branch(pm_env.projects / "demo" / "slot-one", "feat")
    StackerDB(pm_env.stacker_db()).upsert_branch(_tracked("foo", "feat", "main"))
    ps = status_mod.status(pm_env, "demo")
    assert len(ps.stacker) == 1
    info = ps.stacker[0].info
    assert "(slot-one)" in info
    assert "(current)" not in info


def test_stacker_empty_when_no_tracked_branch(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just(["foo"]))
    _checkout_branch(pm_env.projects / "demo" / "foo", "feat")
    # Track some unrelated branch — must not pollute the project's stacker section.
    StackerDB(pm_env.stacker_db()).upsert_branch(
        _tracked("foo", "unrelated", "main"),
    )
    ps = status_mod.status(pm_env, "demo")
    assert ps.worktrees[0].tracked is False
    assert ps.stacker == []
