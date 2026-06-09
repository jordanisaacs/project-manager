import sqlite3
from pathlib import Path

import pytest

# Import for side-effect: registers project sub-app on the root.
import project_manager.cli  # noqa: F401
from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.pool.db import Owner, OwnerKind, PoolDB
from project_manager.pool.slot import PoolExhaustedError, Slot
from project_manager.project import add as add_mod
from project_manager.project import attach as attach_mod
from project_manager.project import branch as branch_mod
from project_manager.project import db
from project_manager.project import detach as detach_mod
from project_manager.project import ls as ls_mod
from project_manager.project import remove as remove_mod
from project_manager.project.cli.delete import delete as cli_project_delete
from project_manager.project.cli.wt.detach import detach as cli_wt_detach
from project_manager.project.spec import parse_wt_spec
from tests.helpers import git_in_slot, git_pool, head_ref, write_git_sentinel


def _mk_pool(paths: Paths, repo: str, uuids: list[str]) -> None:
    """Lightweight slot stub — fine for tests that never exercise git."""
    (paths.worktrees / repo).mkdir()
    for u in uuids:
        (paths.worktrees / repo / u).mkdir()


def _db_rows(paths: Paths, project: str) -> list[tuple[str, str, str]]:
    """Return [(wt, repo, slot_uuid), ...] sorted by wt."""
    with db.readonly(paths.project_db(project)) as conn:
        return db.list_wts(conn)


def _wt_map(paths: Paths, project: str) -> dict[str, tuple[str, str]]:
    """{wt: (repo, slot_uuid)}."""
    return {w: (r, u) for w, r, u in _db_rows(paths, project)}


def _saved_branch(paths: Paths, project: str, wt: str) -> str | None:
    with db.readonly(paths.project_db(project)) as conn:
        return db.get_branch(conn, wt)


def _pool_owner(paths: Paths, repo: str, uuid: str) -> Owner | None:
    return PoolDB(paths.pool_db()).get_owner(repo, uuid)


def _current_uuid(paths: Paths, project: str, wt: str) -> str:
    return _wt_map(paths, project)[wt][1]


def _forward(paths: Paths, project: str, wt: str) -> Path:
    return paths.projects / project / wt


def _just_repos(wts: list[str]) -> list[tuple[str, str]]:
    """Convenience: wt name == repo name (legacy 1:1 mapping)."""
    return [(w, w) for w in wts]


# --- create ---


def test_create_single_repo_creates_db_and_pool_row(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    assert (pm_env.projects / "demo" / ".pm.db").is_file()
    assert _forward(pm_env, "demo", "foo").is_symlink()
    assert _pool_owner(pm_env, "foo", "a") == Owner(OwnerKind.PROJECT, "demo")
    assert _db_rows(pm_env, "demo") == [("foo", "foo", "a")]


def test_create_multi_repo(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x"])
    add_mod.add(pm_env, "demo", _just_repos(["foo", "bar"]))
    assert sorted(_db_rows(pm_env, "demo")) == [
        ("bar", "bar", "x"),
        ("foo", "foo", "a"),
    ]


def test_create_rolls_back_when_second_repo_exhausted(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", [])
    with pytest.raises(PoolExhaustedError):
        add_mod.add(pm_env, "demo", _just_repos(["foo", "bar"]))
    assert _pool_owner(pm_env, "foo", "a") is None
    assert not (pm_env.projects / "demo").exists()


def test_create_fails_if_project_already_has_wt(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a", "b"])
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    with pytest.raises(ProjectError, match="already has worktree"):
        add_mod.add(pm_env, "demo", _just_repos(["foo"]))


def test_create_writes_readme(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    readme = pm_env.projects / "demo" / "README.md"
    assert readme.is_file()
    body = readme.read_text(encoding="utf-8")
    assert "demo" in body
    assert "not a git" in body
    assert "pm project status" in body


def test_create_does_not_overwrite_readme(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x"])
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    readme = pm_env.projects / "demo" / "README.md"
    readme.write_text("user edit", encoding="utf-8")
    add_mod.add(pm_env, "demo", _just_repos(["bar"]))
    assert readme.read_text(encoding="utf-8") == "user edit"


def test_create_rollback_does_not_leave_readme(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", [])
    with pytest.raises(PoolExhaustedError):
        add_mod.add(pm_env, "demo", _just_repos(["foo", "bar"]))
    assert not (pm_env.projects / "demo" / "README.md").exists()
    assert not (pm_env.projects / "demo").exists()


def test_create_empty_wts_just_materializes_project(pm_env: Paths) -> None:
    add_mod.add(pm_env, "demo", [])
    assert (pm_env.projects / "demo" / ".pm.db").is_file()
    assert (pm_env.projects / "demo" / "README.md").is_file()
    assert _db_rows(pm_env, "demo") == []


def test_create_two_wts_same_repo(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a", "b"])
    add_mod.add(pm_env, "demo", [("wt1", "foo"), ("wt2", "foo")])
    rows = sorted(_db_rows(pm_env, "demo"))
    assert [(w, r) for w, r, _ in rows] == [("wt1", "foo"), ("wt2", "foo")]
    uuids = {u for _, _, u in rows}
    assert uuids == {"a", "b"}
    assert _forward(pm_env, "demo", "wt1").is_symlink()
    assert _forward(pm_env, "demo", "wt2").is_symlink()


def test_create_duplicate_wt_in_spec_rejected(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a", "b"])
    with pytest.raises(ProjectError, match="duplicate worktree"):
        add_mod.add(pm_env, "demo", [("wt1", "foo"), ("wt1", "foo")])


def test_create_add_wts_to_existing_project(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x"])
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    add_mod.add(pm_env, "demo", _just_repos(["bar"]))
    wts = {w: r for w, r, _ in _db_rows(pm_env, "demo")}
    assert wts == {"foo": "foo", "bar": "bar"}


# --- detach ---


def test_detach_unlinks_forward_and_releases_pool_row_but_keeps_db(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    uuid = _current_uuid(pm_env, "demo", "foo")
    detach_mod.detach(pm_env, "demo", wts=None)
    assert not _forward(pm_env, "demo", "foo").exists()
    assert _pool_owner(pm_env, "foo", uuid) is None
    assert (pm_env.projects / "demo" / ".pm.db").is_file()
    assert _db_rows(pm_env, "demo") == [("foo", "foo", uuid)]


def test_detach_per_wt(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    git_pool(pm_env, "bar", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo", "bar"]))
    detach_mod.detach(pm_env, "demo", wts=["foo"])
    assert not _forward(pm_env, "demo", "foo").exists()
    assert _forward(pm_env, "demo", "bar").is_symlink()


def test_detach_refuses_unknown_wt(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    with pytest.raises(ProjectError, match="no such worktree"):
        detach_mod.detach(pm_env, "demo", wts=["nope"])


def test_detach_saves_current_branch(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    slot = _forward(pm_env, "demo", "foo").resolve()
    git_in_slot(slot, "checkout", "-b", "feature-x")
    detach_mod.detach(pm_env, "demo", wts=None)
    assert _saved_branch(pm_env, "demo", "foo") == "feature-x"


def test_detach_saves_null_when_detached(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    # Slots are already in detached HEAD after `pool.add`.
    detach_mod.detach(pm_env, "demo", wts=None)
    assert _saved_branch(pm_env, "demo", "foo") is None


def test_detach_blocks_on_dirty(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    slot = _forward(pm_env, "demo", "foo").resolve()
    # stage an uncommitted change
    (slot / "README.md").write_text("dirty\n")
    git_in_slot(slot, "add", "README.md")
    uuid = _current_uuid(pm_env, "demo", "foo")
    with pytest.raises(ProjectError, match="uncommitted changes"):
        detach_mod.detach(pm_env, "demo", wts=None)
    # Slot stays attached: symlink still present, pool row still owned, branch field untouched.
    assert _forward(pm_env, "demo", "foo").is_symlink()
    assert _pool_owner(pm_env, "foo", uuid) == Owner(OwnerKind.PROJECT, "demo")
    assert _saved_branch(pm_env, "demo", "foo") is None


def test_detach_multi_wt_partial_when_one_dirty(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    git_pool(pm_env, "bar", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["bar", "foo"]))
    foo_slot = _forward(pm_env, "demo", "foo").resolve()
    (foo_slot / "README.md").write_text("dirty\n")
    git_in_slot(foo_slot, "add", "README.md")
    with pytest.raises(ProjectError, match="uncommitted changes"):
        detach_mod.detach(pm_env, "demo", wts=["bar", "foo"])
    assert not _forward(pm_env, "demo", "bar").exists()
    assert _forward(pm_env, "demo", "foo").is_symlink()


def test_detach_parks_slot_to_default(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1, branch="main")
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    slot = _forward(pm_env, "demo", "foo").resolve()
    git_in_slot(slot, "checkout", "-b", "feature-x")
    detach_mod.detach(pm_env, "demo", wts=None)
    # Slot is detached HEAD at main's tip after park.
    assert head_ref(slot) is None
    head_sha = git_in_slot(slot, "rev-parse", "HEAD").stdout.strip()
    main_sha = git_in_slot(pm_env.repo("foo"), "rev-parse", "main").stdout.strip()
    assert head_sha == main_sha


# --- attach ---


def test_attach_reclaims_same_slot_when_free(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=2)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    rows_before = _db_rows(pm_env, "demo")
    detach_mod.detach(pm_env, "demo", wts=None)
    attach_mod.attach(pm_env, "demo", wts=None)
    assert _db_rows(pm_env, "demo") == rows_before  # same uuid
    assert _forward(pm_env, "demo", "foo").readlink().name == rows_before[0][2]


def test_attach_falls_back_and_updates_row(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=2)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    remembered = _current_uuid(pm_env, "demo", "foo")
    detach_mod.detach(pm_env, "demo", wts=None)
    # steal demo's remembered slot
    add_mod.add(pm_env, "other", _just_repos(["foo"]))
    # demo's attach should fall back
    attach_mod.attach(pm_env, "demo", wts=None)
    new_uuid = _current_uuid(pm_env, "demo", "foo")
    assert new_uuid != remembered
    assert _forward(pm_env, "demo", "foo").readlink().name == new_uuid


def test_attach_errors_when_pool_exhausted_for_fallback(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    detach_mod.detach(pm_env, "demo", wts=None)
    add_mod.add(pm_env, "other", _just_repos(["foo"]))
    with pytest.raises(PoolExhaustedError):
        attach_mod.attach(pm_env, "demo", wts=None)


def test_attach_partial_and_all(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    git_pool(pm_env, "bar", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo", "bar"]))
    detach_mod.detach(pm_env, "demo", wts=None)
    attach_mod.attach(pm_env, "demo", wts=["foo"])
    assert _forward(pm_env, "demo", "foo").is_symlink()
    assert not _forward(pm_env, "demo", "bar").is_symlink()
    attach_mod.attach(pm_env, "demo", wts=None)
    assert _forward(pm_env, "demo", "bar").is_symlink()


def test_attach_idempotent_when_already_attached(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    result = attach_mod.attach(pm_env, "demo", wts=None)
    assert result.newly_claimed == []
    assert result.warnings == []


def test_attach_errors_on_nonexistent_project(pm_env: Paths) -> None:
    with pytest.raises(ProjectError, match="does not exist"):
        attach_mod.attach(pm_env, "ghost", wts=None)


# --- attach branch restore ---


def test_attach_restores_branch(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    slot = _forward(pm_env, "demo", "foo").resolve()
    git_in_slot(slot, "checkout", "-b", "feature-x")
    detach_mod.detach(pm_env, "demo", wts=None)
    attach_mod.attach(pm_env, "demo", wts=None)
    restored = _forward(pm_env, "demo", "foo").resolve()
    assert head_ref(restored) == "feature-x"


def test_attach_clears_saved_branch_on_success(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    slot = _forward(pm_env, "demo", "foo").resolve()
    git_in_slot(slot, "checkout", "-b", "feature-x")
    detach_mod.detach(pm_env, "demo", wts=None)
    assert _saved_branch(pm_env, "demo", "foo") == "feature-x"
    attach_mod.attach(pm_env, "demo", wts=None)
    assert _saved_branch(pm_env, "demo", "foo") is None


def test_attach_no_branch_flag_skips_restore_but_clears(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    slot = _forward(pm_env, "demo", "foo").resolve()
    git_in_slot(slot, "checkout", "-b", "feature-x")
    detach_mod.detach(pm_env, "demo", wts=None)
    attach_mod.attach(pm_env, "demo", wts=None, no_branch=True)
    restored = _forward(pm_env, "demo", "foo").resolve()
    assert head_ref(restored) is None  # still detached
    assert _saved_branch(pm_env, "demo", "foo") is None


def test_attach_warns_when_branch_in_use(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    slot = _forward(pm_env, "demo", "foo").resolve()
    git_in_slot(slot, "checkout", "-b", "feature-x")
    detach_mod.detach(pm_env, "demo", wts=None)
    # Create a second worktree holding the same branch.
    stolen_wt = pm_env.worktrees / "foo-stolen"
    git_in_slot(pm_env.repo("foo"), "worktree", "add", str(stolen_wt), "feature-x")
    try:
        result = attach_mod.attach(pm_env, "demo", wts=None)
        assert len(result.warnings) == 1
        assert "feature-x" in result.warnings[0]
        assert "already checked out" in result.warnings[0]
        restored = _forward(pm_env, "demo", "foo").resolve()
        assert head_ref(restored) is None  # left detached
        assert _saved_branch(pm_env, "demo", "foo") is None
    finally:
        git_in_slot(pm_env.repo("foo"), "worktree", "remove", "--force", str(stolen_wt))


def test_attach_missing_branch_warns(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    slot = _forward(pm_env, "demo", "foo").resolve()
    git_in_slot(slot, "checkout", "-b", "feature-x")
    detach_mod.detach(pm_env, "demo", wts=None)
    # Delete the branch from the main repo entirely.
    git_in_slot(pm_env.repo("foo"), "branch", "-D", "feature-x")
    result = attach_mod.attach(pm_env, "demo", wts=None)
    assert len(result.warnings) == 1
    assert "no longer exists" in result.warnings[0]
    restored = _forward(pm_env, "demo", "foo").resolve()
    assert head_ref(restored) is None
    assert _saved_branch(pm_env, "demo", "foo") is None


def test_attach_no_saved_branch_is_noop(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    # Never checked out a branch; detach while detached saves NULL.
    detach_mod.detach(pm_env, "demo", wts=None)
    result = attach_mod.attach(pm_env, "demo", wts=None)
    assert result.warnings == []
    restored = _forward(pm_env, "demo", "foo").resolve()
    assert head_ref(restored) is None


# --- delete ---


def test_delete_whole_removes_db_and_dir(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    uuid = _current_uuid(pm_env, "demo", "foo")
    remove_mod.remove(pm_env, "demo", wts=None)
    assert not (pm_env.projects / "demo").exists()
    assert _pool_owner(pm_env, "foo", uuid) is None


def test_delete_whole_removes_readme(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    assert (pm_env.projects / "demo" / "README.md").is_file()
    remove_mod.remove(pm_env, "demo", wts=None)
    assert not (pm_env.projects / "demo").exists()


def test_delete_per_wt_implicit_detach(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    git_pool(pm_env, "bar", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo", "bar"]))
    foo_uuid = _current_uuid(pm_env, "demo", "foo")
    bar_uuid = _current_uuid(pm_env, "demo", "bar")
    remove_mod.remove(pm_env, "demo", wts=["foo"])
    assert not _forward(pm_env, "demo", "foo").exists()
    assert _pool_owner(pm_env, "foo", foo_uuid) is None
    assert _forward(pm_env, "demo", "bar").is_symlink()
    assert _db_rows(pm_env, "demo") == [("bar", "bar", bar_uuid)]


def test_delete_per_wt_when_already_detached(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    detach_mod.detach(pm_env, "demo", wts=None)
    remove_mod.remove(pm_env, "demo", wts=["foo"])
    assert _db_rows(pm_env, "demo") == []


def test_delete_whole_refuses_extra_files(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    (pm_env.projects / "demo" / "notes.txt").write_text("hi")
    with pytest.raises(ProjectError, match="non-pm entries"):
        remove_mod.remove(pm_env, "demo", wts=None)
    # project still intact
    assert (pm_env.projects / "demo" / ".pm.db").is_file()


def test_delete_refuses_unknown_wt(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    with pytest.raises(ProjectError, match="no such worktree"):
        remove_mod.remove(pm_env, "demo", wts=["nope"])


# --- ls ---


def test_ls_reports_mixed_states(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    git_pool(pm_env, "bar", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo", "bar"]))
    attached_foo_branch = head_ref(_forward(pm_env, "demo", "foo").resolve())
    saved_bar_branch = head_ref(_forward(pm_env, "demo", "bar").resolve())
    detach_mod.detach(pm_env, "demo", wts=["bar"])
    rows = ls_mod.ls(pm_env)
    by_wt = {r.wt: r for r in rows}
    assert by_wt["foo"].status == "attached"
    assert by_wt["bar"].status == "detached"
    # Attached row reflects whatever the slot's HEAD is right now;
    # detached row falls back to the saved branch from the project db.
    assert by_wt["foo"].branch == (attached_foo_branch or "(detached)")
    assert by_wt["bar"].branch == (saved_bar_branch or "-")


def test_ls_drift(pm_env: Paths) -> None:
    slots = git_pool(pm_env, "foo", n=2)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    original_uuid = _current_uuid(pm_env, "demo", "foo")
    other_slot: Slot = next(s for s in slots if s.uuid != original_uuid)
    detach_mod.detach(pm_env, "demo", wts=None)
    PoolDB(pm_env.pool_db()).claim("foo", other_slot.uuid, Owner(OwnerKind.PROJECT, "demo"))
    _forward(pm_env, "demo", "foo").symlink_to(other_slot.path)
    rows = ls_mod.ls(pm_env)
    assert rows[0].status == "drift"
    # Drift still reads the live slot's HEAD for the Branch column.
    assert rows[0].branch == (head_ref(other_slot.path) or "(detached)")


# --- branch module unit-ish tests ---


def test_branch_ensure_clean_passes_when_clean(pm_env: Paths) -> None:
    slots = git_pool(pm_env, "foo", n=1)
    branch_mod.ensure_clean(slots[0].path, "foo")  # no raise


def test_branch_ensure_clean_raises_when_dirty(pm_env: Paths) -> None:
    slots = git_pool(pm_env, "foo", n=1)
    (slots[0].path / "README.md").write_text("dirty\n")
    git_in_slot(slots[0].path, "add", "README.md")
    with pytest.raises(ProjectError, match="uncommitted changes"):
        branch_mod.ensure_clean(slots[0].path, "foo")


# --- cleanliness: untracked, ignored, stash, in-progress ops ---


def test_detach_blocks_on_untracked(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    slot = _forward(pm_env, "demo", "foo").resolve()
    (slot / "scratch.log").write_text("noise\n")
    uuid = _current_uuid(pm_env, "demo", "foo")
    with pytest.raises(ProjectError, match="untracked"):
        detach_mod.detach(pm_env, "demo", wts=None)
    assert _forward(pm_env, "demo", "foo").is_symlink()
    assert _pool_owner(pm_env, "foo", uuid) == Owner(OwnerKind.PROJECT, "demo")


def test_detach_allows_ignored_files(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    slot = _forward(pm_env, "demo", "foo").resolve()
    (slot / ".gitignore").write_text("*.log\n")
    git_in_slot(slot, "add", ".gitignore")
    git_in_slot(
        slot,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-m",
        "ignore logs",
    )
    (slot / "build.log").write_text("ignored\n")
    detach_mod.detach(pm_env, "demo", wts=None)
    assert not _forward(pm_env, "demo", "foo").exists()


def test_detach_allows_stash(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    slot = _forward(pm_env, "demo", "foo").resolve()
    git_in_slot(slot, "checkout", "-b", "feature-x")
    (slot / "README.md").write_text("stashable\n")
    git_in_slot(slot, "stash", "push", "-m", "wip")
    detach_mod.detach(pm_env, "demo", wts=None)
    assert not _forward(pm_env, "demo", "foo").exists()


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("MERGE_HEAD", "merge"),
        ("REVERT_HEAD", "revert"),
        ("BISECT_LOG", "bisect"),
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
    ],
)
def test_detach_blocks_on_in_progress_op(
    pm_env: Paths,
    marker: str,
    expected: str,
) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    slot = _forward(pm_env, "demo", "foo").resolve()
    write_git_sentinel(slot, marker)
    with pytest.raises(ProjectError, match=f"{expected} in progress"):
        detach_mod.detach(pm_env, "demo", wts=None)
    assert _forward(pm_env, "demo", "foo").is_symlink()


# --- delete layered on detach ---


def test_delete_per_wt_blocks_on_dirty(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    slot = _forward(pm_env, "demo", "foo").resolve()
    (slot / "README.md").write_text("dirty\n")
    git_in_slot(slot, "add", "README.md")
    with pytest.raises(ProjectError, match="uncommitted changes"):
        remove_mod.remove(pm_env, "demo", wts=["foo"])
    assert _db_rows(pm_env, "demo") == [("foo", "foo", _current_uuid(pm_env, "demo", "foo"))]
    assert _forward(pm_env, "demo", "foo").is_symlink()


def test_delete_whole_blocks_on_dirty(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    slot = _forward(pm_env, "demo", "foo").resolve()
    (slot / "README.md").write_text("dirty\n")
    git_in_slot(slot, "add", "README.md")
    with pytest.raises(ProjectError, match="uncommitted changes"):
        remove_mod.remove(pm_env, "demo", wts=None)
    assert (pm_env.projects / "demo" / ".pm.db").is_file()
    assert _forward(pm_env, "demo", "foo").is_symlink()


def test_delete_whole_blocks_on_untracked(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    slot = _forward(pm_env, "demo", "foo").resolve()
    (slot / "scratch.log").write_text("noise\n")
    with pytest.raises(ProjectError, match="untracked"):
        remove_mod.remove(pm_env, "demo", wts=None)
    assert (pm_env.projects / "demo" / ".pm.db").is_file()


def test_delete_whole_blocks_on_in_progress_rebase(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    slot = _forward(pm_env, "demo", "foo").resolve()
    write_git_sentinel(slot, "rebase-merge")
    with pytest.raises(ProjectError, match="rebase in progress"):
        remove_mod.remove(pm_env, "demo", wts=None)
    assert (pm_env.projects / "demo" / ".pm.db").is_file()


# --- dry-run: plan_detach, plan_remove ---


def test_plan_detach_clean_project(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    git_pool(pm_env, "bar", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo", "bar"]))
    plan = detach_mod.plan_detach(pm_env, "demo", wts=None)
    assert not plan.has_blocker
    kinds = {a.wt: a.kind for a in plan.actions}
    assert kinds == {"foo": "detach", "bar": "detach"}
    assert all(a.blocker is None for a in plan.actions)
    assert _forward(pm_env, "demo", "foo").is_symlink()
    assert _forward(pm_env, "demo", "bar").is_symlink()


def test_plan_detach_reports_blockers_without_mutating(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    git_pool(pm_env, "bar", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo", "bar"]))
    foo_slot = _forward(pm_env, "demo", "foo").resolve()
    (foo_slot / "scratch.log").write_text("noise\n")
    bar_slot = _forward(pm_env, "demo", "bar").resolve()
    write_git_sentinel(bar_slot, "CHERRY_PICK_HEAD")
    plan = detach_mod.plan_detach(pm_env, "demo", wts=None)
    assert plan.has_blocker
    by_wt = {a.wt: a for a in plan.actions}
    assert by_wt["foo"].blocker is not None
    assert "untracked" in by_wt["foo"].blocker
    assert by_wt["bar"].blocker is not None
    assert "cherry-pick" in by_wt["bar"].blocker
    assert _forward(pm_env, "demo", "foo").is_symlink()
    assert _forward(pm_env, "demo", "bar").is_symlink()


def test_plan_detach_noop_for_already_detached(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    detach_mod.detach(pm_env, "demo", wts=None)
    plan = detach_mod.plan_detach(pm_env, "demo", wts=None)
    assert not plan.has_blocker
    assert plan.actions[0].kind == "noop"
    assert plan.actions[0].blocker is None


def test_plan_remove_whole_reports_extras(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    (pm_env.projects / "demo" / "notes.txt").write_text("hi")
    plan = remove_mod.plan_remove(pm_env, "demo", wts=None)
    assert plan.has_blocker
    assert any(p.name == "notes.txt" for p in plan.extras)
    assert plan.remove_readme
    assert plan.drop_db
    assert plan.rmdir
    assert (pm_env.projects / "demo" / "notes.txt").exists()


def test_plan_remove_whole_clean(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    plan = remove_mod.plan_remove(pm_env, "demo", wts=None)
    assert not plan.has_blocker
    assert plan.whole
    assert plan.drop_rows == ["foo"]
    assert plan.remove_readme
    assert plan.drop_db
    assert plan.rmdir
    assert (pm_env.projects / "demo" / ".pm.db").is_file()


def test_plan_remove_per_wt(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    git_pool(pm_env, "bar", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo", "bar"]))
    plan = remove_mod.plan_remove(pm_env, "demo", wts=["foo"])
    assert not plan.whole
    assert plan.drop_rows == ["foo"]
    assert not plan.remove_readme
    assert not plan.drop_db
    assert not plan.rmdir
    assert [a.wt for a in plan.detach_plan.actions] == ["foo"]


# --- dry-run CLI exit codes ---


def test_cli_wt_detach_dry_run_exits_zero_when_clean(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    from project_manager.cli._shared import ProjectFlag, WtSelection

    rc = cli_wt_detach(
        flag=ProjectFlag(project="demo"),
        sel=WtSelection(all=True),
        dry_run=True,
    )
    assert rc == 0
    assert _forward(pm_env, "demo", "foo").is_symlink()


def test_cli_wt_detach_dry_run_exits_one_on_blocker(
    pm_env: Paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    slot = _forward(pm_env, "demo", "foo").resolve()
    (slot / "scratch.log").write_text("noise\n")
    from project_manager.cli._shared import ProjectFlag, WtSelection

    rc = cli_wt_detach(
        flag=ProjectFlag(project="demo"),
        sel=WtSelection(all=True),
        dry_run=True,
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "BLOCKED" in err
    assert _forward(pm_env, "demo", "foo").is_symlink()


def test_cli_project_delete_dry_run_exits_one_on_extras(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    add_mod.add(pm_env, "demo", _just_repos(["foo"]))
    (pm_env.projects / "demo" / "notes.txt").write_text("hi")
    rc = cli_project_delete("demo", dry_run=True)
    assert rc == 1
    assert (pm_env.projects / "demo" / "notes.txt").exists()
    assert (pm_env.projects / "demo" / ".pm.db").is_file()


# --- spec parsing (CLI) ---


def test_parse_wt_spec_bare_repo() -> None:
    assert parse_wt_spec("foo") == [("foo", "foo")]


def test_parse_wt_spec_named_and_bare() -> None:
    assert parse_wt_spec("foo,bar:baz,qux") == [
        ("foo", "foo"),
        ("bar", "baz"),
        ("qux", "qux"),
    ]


def test_parse_wt_spec_rejects_duplicates() -> None:
    with pytest.raises(ProjectError, match="duplicate worktree"):
        parse_wt_spec("foo,foo:bar")


def test_parse_wt_spec_rejects_empty_side() -> None:
    with pytest.raises(ProjectError, match="empty name or repo"):
        parse_wt_spec("foo,:bar")
    with pytest.raises(ProjectError, match="empty name or repo"):
        parse_wt_spec("foo,bar:")


def test_parse_wt_spec_rejects_extra_colons() -> None:
    with pytest.raises(ProjectError, match="too many colons"):
        parse_wt_spec("foo:bar:baz")


# --- db migration from legacy `repos` table ---


def test_migration_repos_to_worktrees(pm_env: Paths) -> None:
    db_path = pm_env.project_db("legacy")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE repos (  name TEXT PRIMARY KEY,  slot_uuid TEXT NOT NULL,  branch TEXT)"
        )
        conn.execute(
            "INSERT INTO repos (name, slot_uuid, branch) VALUES (?, ?, ?)",
            ("foo", "abc123", "feature-x"),
        )
        conn.execute(
            "INSERT INTO repos (name, slot_uuid, branch) VALUES (?, ?, NULL)",
            ("bar", "def456"),
        )
        conn.commit()
    finally:
        conn.close()

    # Opening through db.readonly should trigger the migration.
    with db.readonly(db_path) as conn:
        rows = db.list_wts(conn)
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

    assert "repos" not in tables
    assert "worktrees" in tables
    assert sorted(rows) == [("bar", "bar", "def456"), ("foo", "foo", "abc123")]
    with db.readonly(db_path) as conn:
        assert db.get_branch(conn, "foo") == "feature-x"
        assert db.get_branch(conn, "bar") is None
