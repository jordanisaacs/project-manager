import argparse
from pathlib import Path

import pytest

from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.pool.db import Owner, OwnerKind, PoolDB
from project_manager.pool.slot import PoolExhaustedError, Slot
from project_manager.project import attach as attach_mod
from project_manager.project import branch as branch_mod
from project_manager.project import cli as project_cli
from project_manager.project import db
from project_manager.project import delete as delete_mod
from project_manager.project import detach as detach_mod
from project_manager.project import ls as ls_mod
from project_manager.project import new as new_mod
from tests.helpers import git_in_slot, git_pool, head_ref, write_git_sentinel


def _mk_pool(paths: Paths, repo: str, uuids: list[str]) -> None:
    """Lightweight slot stub — fine for tests that never exercise git."""
    (paths.worktrees / repo).mkdir()
    for u in uuids:
        (paths.worktrees / repo / u).mkdir()


def _db_rows(paths: Paths, project: str) -> list[tuple[str, str]]:
    with db.readonly(paths.project_db(project)) as conn:
        return db.list_repos(conn)


def _saved_branch(paths: Paths, project: str, repo: str) -> str | None:
    with db.readonly(paths.project_db(project)) as conn:
        return db.get_branch(conn, repo)


def _pool_owner(paths: Paths, repo: str, uuid: str) -> Owner | None:
    return PoolDB(paths.pool_db()).get_owner(repo, uuid)


def _current_uuid(paths: Paths, project: str, repo: str) -> str:
    rows = dict(_db_rows(paths, project))
    return rows[repo]


def _forward(paths: Paths, project: str, repo: str) -> Path:
    return paths.projects / project / repo


# --- new ---


def test_new_single_repo_creates_db_and_pool_row(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    assert (pm_env.projects / "demo" / ".pm.db").is_file()
    assert _forward(pm_env, "demo", "foo").is_symlink()
    assert _pool_owner(pm_env, "foo", "a") == Owner(OwnerKind.PROJECT, "demo")
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
    assert _pool_owner(pm_env, "foo", "a") is None
    assert not (pm_env.projects / "demo").exists()


def test_new_fails_if_project_already_has_repo(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a", "b"])
    new_mod.new(pm_env, "demo", ["foo"])
    with pytest.raises(ProjectError, match="already has"):
        new_mod.new(pm_env, "demo", ["foo"])


def test_new_writes_readme(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    new_mod.new(pm_env, "demo", ["foo"])
    readme = pm_env.projects / "demo" / "README.md"
    assert readme.is_file()
    body = readme.read_text(encoding="utf-8")
    assert "demo" in body
    assert "not a git" in body
    assert "pm project status" in body


def test_new_does_not_overwrite_readme(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", ["x"])
    new_mod.new(pm_env, "demo", ["foo"])
    readme = pm_env.projects / "demo" / "README.md"
    readme.write_text("user edit", encoding="utf-8")
    new_mod.new(pm_env, "demo", ["bar"])
    assert readme.read_text(encoding="utf-8") == "user edit"


def test_new_rollback_does_not_leave_readme(pm_env: Paths) -> None:
    _mk_pool(pm_env, "foo", ["a"])
    _mk_pool(pm_env, "bar", [])
    with pytest.raises(PoolExhaustedError):
        new_mod.new(pm_env, "demo", ["foo", "bar"])
    assert not (pm_env.projects / "demo" / "README.md").exists()
    assert not (pm_env.projects / "demo").exists()


# --- detach ---


def test_detach_unlinks_forward_and_releases_pool_row_but_keeps_db(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    uuid = _current_uuid(pm_env, "demo", "foo")
    detach_mod.detach(pm_env, "demo", repos=None)
    assert not _forward(pm_env, "demo", "foo").exists()
    assert _pool_owner(pm_env, "foo", uuid) is None
    assert (pm_env.projects / "demo" / ".pm.db").is_file()
    assert _db_rows(pm_env, "demo") == [("foo", uuid)]


def test_detach_per_repo(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    git_pool(pm_env, "bar", n=1)
    new_mod.new(pm_env, "demo", ["foo", "bar"])
    detach_mod.detach(pm_env, "demo", repos=["foo"])
    assert not _forward(pm_env, "demo", "foo").exists()
    assert _forward(pm_env, "demo", "bar").is_symlink()


def test_detach_refuses_unknown_repo(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    with pytest.raises(ProjectError, match="no such repo"):
        detach_mod.detach(pm_env, "demo", repos=["nope"])


def test_detach_saves_current_branch(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    slot = _forward(pm_env, "demo", "foo").resolve()
    git_in_slot(slot, "checkout", "-b", "feature-x")
    detach_mod.detach(pm_env, "demo", repos=None)
    assert _saved_branch(pm_env, "demo", "foo") == "feature-x"


def test_detach_saves_null_when_detached(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    # Slots are already in detached HEAD after `pool.add`.
    detach_mod.detach(pm_env, "demo", repos=None)
    assert _saved_branch(pm_env, "demo", "foo") is None


def test_detach_blocks_on_dirty(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    slot = _forward(pm_env, "demo", "foo").resolve()
    # stage an uncommitted change
    (slot / "README.md").write_text("dirty\n")
    git_in_slot(slot, "add", "README.md")
    uuid = _current_uuid(pm_env, "demo", "foo")
    with pytest.raises(ProjectError, match="uncommitted changes"):
        detach_mod.detach(pm_env, "demo", repos=None)
    # Slot stays attached: symlink still present, pool row still owned, branch field untouched.
    assert _forward(pm_env, "demo", "foo").is_symlink()
    assert _pool_owner(pm_env, "foo", uuid) == Owner(OwnerKind.PROJECT, "demo")
    assert _saved_branch(pm_env, "demo", "foo") is None


def test_detach_multi_repo_partial_when_one_dirty(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    git_pool(pm_env, "bar", n=1)
    new_mod.new(pm_env, "demo", ["bar", "foo"])
    foo_slot = _forward(pm_env, "demo", "foo").resolve()
    (foo_slot / "README.md").write_text("dirty\n")
    git_in_slot(foo_slot, "add", "README.md")
    # detach iterates in argparse/list order; we request [bar, foo] so bar succeeds first,
    # then foo fails — bar is left detached, foo stays attached.
    with pytest.raises(ProjectError, match="uncommitted changes"):
        detach_mod.detach(pm_env, "demo", repos=["bar", "foo"])
    assert not _forward(pm_env, "demo", "bar").exists()
    assert _forward(pm_env, "demo", "foo").is_symlink()


def test_detach_parks_slot_to_default(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1, branch="main")
    new_mod.new(pm_env, "demo", ["foo"])
    slot = _forward(pm_env, "demo", "foo").resolve()
    git_in_slot(slot, "checkout", "-b", "feature-x")
    detach_mod.detach(pm_env, "demo", repos=None)
    # Slot is detached HEAD at main's tip after park.
    assert head_ref(slot) is None
    head_sha = git_in_slot(slot, "rev-parse", "HEAD").stdout.strip()
    main_sha = git_in_slot(pm_env.repo("foo"), "rev-parse", "main").stdout.strip()
    assert head_sha == main_sha


# --- attach ---


def test_attach_reclaims_same_slot_when_free(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=2)
    new_mod.new(pm_env, "demo", ["foo"])
    rows_before = _db_rows(pm_env, "demo")
    detach_mod.detach(pm_env, "demo", repos=None)
    attach_mod.attach(pm_env, "demo", repos=None)
    assert _db_rows(pm_env, "demo") == rows_before  # same uuid
    assert _forward(pm_env, "demo", "foo").readlink().name == rows_before[0][1]


def test_attach_falls_back_and_updates_row(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=2)
    new_mod.new(pm_env, "demo", ["foo"])
    remembered = _db_rows(pm_env, "demo")[0][1]
    detach_mod.detach(pm_env, "demo", repos=None)
    # steal demo's remembered slot
    new_mod.new(pm_env, "other", ["foo"])
    # demo's attach should fall back
    attach_mod.attach(pm_env, "demo", repos=None)
    new_uuid = _db_rows(pm_env, "demo")[0][1]
    assert new_uuid != remembered
    assert _forward(pm_env, "demo", "foo").readlink().name == new_uuid


def test_attach_errors_when_pool_exhausted_for_fallback(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    detach_mod.detach(pm_env, "demo", repos=None)
    new_mod.new(pm_env, "other", ["foo"])
    with pytest.raises(PoolExhaustedError):
        attach_mod.attach(pm_env, "demo", repos=None)


def test_attach_partial_and_all(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    git_pool(pm_env, "bar", n=1)
    new_mod.new(pm_env, "demo", ["foo", "bar"])
    detach_mod.detach(pm_env, "demo", repos=None)
    attach_mod.attach(pm_env, "demo", repos=["foo"])
    assert _forward(pm_env, "demo", "foo").is_symlink()
    assert not _forward(pm_env, "demo", "bar").is_symlink()
    attach_mod.attach(pm_env, "demo", repos=None)
    assert _forward(pm_env, "demo", "bar").is_symlink()


def test_attach_idempotent_when_already_attached(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    result = attach_mod.attach(pm_env, "demo", repos=None)
    assert result.newly_claimed == []
    assert result.warnings == []


def test_attach_errors_on_nonexistent_project(pm_env: Paths) -> None:
    with pytest.raises(ProjectError, match="does not exist"):
        attach_mod.attach(pm_env, "ghost", repos=None)


# --- attach branch restore ---


def test_attach_restores_branch(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    slot = _forward(pm_env, "demo", "foo").resolve()
    git_in_slot(slot, "checkout", "-b", "feature-x")
    detach_mod.detach(pm_env, "demo", repos=None)
    attach_mod.attach(pm_env, "demo", repos=None)
    restored = _forward(pm_env, "demo", "foo").resolve()
    assert head_ref(restored) == "feature-x"


def test_attach_clears_saved_branch_on_success(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    slot = _forward(pm_env, "demo", "foo").resolve()
    git_in_slot(slot, "checkout", "-b", "feature-x")
    detach_mod.detach(pm_env, "demo", repos=None)
    assert _saved_branch(pm_env, "demo", "foo") == "feature-x"
    attach_mod.attach(pm_env, "demo", repos=None)
    assert _saved_branch(pm_env, "demo", "foo") is None


def test_attach_no_branch_flag_skips_restore_but_clears(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    slot = _forward(pm_env, "demo", "foo").resolve()
    git_in_slot(slot, "checkout", "-b", "feature-x")
    detach_mod.detach(pm_env, "demo", repos=None)
    attach_mod.attach(pm_env, "demo", repos=None, no_branch=True)
    restored = _forward(pm_env, "demo", "foo").resolve()
    assert head_ref(restored) is None  # still detached
    assert _saved_branch(pm_env, "demo", "foo") is None


def test_attach_warns_when_branch_in_use(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    slot = _forward(pm_env, "demo", "foo").resolve()
    git_in_slot(slot, "checkout", "-b", "feature-x")
    detach_mod.detach(pm_env, "demo", repos=None)
    # Create a second worktree holding the same branch.
    stolen_wt = pm_env.worktrees / "foo-stolen"
    git_in_slot(pm_env.repo("foo"), "worktree", "add", str(stolen_wt), "feature-x")
    try:
        result = attach_mod.attach(pm_env, "demo", repos=None)
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
    new_mod.new(pm_env, "demo", ["foo"])
    slot = _forward(pm_env, "demo", "foo").resolve()
    git_in_slot(slot, "checkout", "-b", "feature-x")
    detach_mod.detach(pm_env, "demo", repos=None)
    # Delete the branch from the main repo entirely.
    git_in_slot(pm_env.repo("foo"), "branch", "-D", "feature-x")
    result = attach_mod.attach(pm_env, "demo", repos=None)
    assert len(result.warnings) == 1
    assert "no longer exists" in result.warnings[0]
    restored = _forward(pm_env, "demo", "foo").resolve()
    assert head_ref(restored) is None
    assert _saved_branch(pm_env, "demo", "foo") is None


def test_attach_no_saved_branch_is_noop(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    # Never checked out a branch; detach while detached saves NULL.
    detach_mod.detach(pm_env, "demo", repos=None)
    result = attach_mod.attach(pm_env, "demo", repos=None)
    assert result.warnings == []
    restored = _forward(pm_env, "demo", "foo").resolve()
    assert head_ref(restored) is None


# --- delete ---


def test_delete_whole_removes_db_and_dir(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    uuid = _current_uuid(pm_env, "demo", "foo")
    delete_mod.delete(pm_env, "demo", repos=None)
    assert not (pm_env.projects / "demo").exists()
    assert _pool_owner(pm_env, "foo", uuid) is None


def test_delete_whole_removes_readme(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    assert (pm_env.projects / "demo" / "README.md").is_file()
    delete_mod.delete(pm_env, "demo", repos=None)
    assert not (pm_env.projects / "demo").exists()


def test_delete_per_repo_implicit_detach(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    git_pool(pm_env, "bar", n=1)
    new_mod.new(pm_env, "demo", ["foo", "bar"])
    foo_uuid = _current_uuid(pm_env, "demo", "foo")
    bar_uuid = _current_uuid(pm_env, "demo", "bar")
    delete_mod.delete(pm_env, "demo", repos=["foo"])
    assert not _forward(pm_env, "demo", "foo").exists()
    assert _pool_owner(pm_env, "foo", foo_uuid) is None
    assert _forward(pm_env, "demo", "bar").is_symlink()
    assert _db_rows(pm_env, "demo") == [("bar", bar_uuid)]


def test_delete_per_repo_when_already_detached(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    detach_mod.detach(pm_env, "demo", repos=None)
    delete_mod.delete(pm_env, "demo", repos=["foo"])
    assert _db_rows(pm_env, "demo") == []


def test_delete_whole_refuses_extra_files(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    (pm_env.projects / "demo" / "notes.txt").write_text("hi")
    with pytest.raises(ProjectError, match="non-pm entries"):
        delete_mod.delete(pm_env, "demo", repos=None)
    # project still intact
    assert (pm_env.projects / "demo" / ".pm.db").is_file()


def test_delete_refuses_unknown_repo(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    with pytest.raises(ProjectError, match="no such repo"):
        delete_mod.delete(pm_env, "demo", repos=["nope"])


# --- ls ---


def test_ls_reports_mixed_states(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    git_pool(pm_env, "bar", n=1)
    new_mod.new(pm_env, "demo", ["foo", "bar"])
    detach_mod.detach(pm_env, "demo", repos=["bar"])
    rows = ls_mod.ls(pm_env)
    by_repo = {r.repo: r for r in rows}
    assert by_repo["foo"].status == "attached"
    assert by_repo["bar"].status == "detached"


def test_ls_drift(pm_env: Paths) -> None:
    slots = git_pool(pm_env, "foo", n=2)
    new_mod.new(pm_env, "demo", ["foo"])
    # detach, then manually claim the OTHER slot in the pool db and forward-link
    # to it while the project db still remembers the original.
    original_uuid = _current_uuid(pm_env, "demo", "foo")
    other_slot: Slot = next(s for s in slots if s.uuid != original_uuid)
    detach_mod.detach(pm_env, "demo", repos=None)
    PoolDB(pm_env.pool_db()).claim("foo", other_slot.uuid, Owner(OwnerKind.PROJECT, "demo"))
    _forward(pm_env, "demo", "foo").symlink_to(other_slot.path)
    rows = ls_mod.ls(pm_env)
    assert rows[0].status == "drift"


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
    new_mod.new(pm_env, "demo", ["foo"])
    slot = _forward(pm_env, "demo", "foo").resolve()
    (slot / "scratch.log").write_text("noise\n")
    uuid = _current_uuid(pm_env, "demo", "foo")
    with pytest.raises(ProjectError, match="untracked"):
        detach_mod.detach(pm_env, "demo", repos=None)
    assert _forward(pm_env, "demo", "foo").is_symlink()
    assert _pool_owner(pm_env, "foo", uuid) == Owner(OwnerKind.PROJECT, "demo")


def test_detach_allows_ignored_files(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    slot = _forward(pm_env, "demo", "foo").resolve()
    (slot / ".gitignore").write_text("*.log\n")
    git_in_slot(slot, "add", ".gitignore")
    git_in_slot(slot, "-c", "user.email=t@t", "-c", "user.name=t",
                "-c", "commit.gpgsign=false", "commit", "-m", "ignore logs")
    (slot / "build.log").write_text("ignored\n")
    detach_mod.detach(pm_env, "demo", repos=None)
    assert not _forward(pm_env, "demo", "foo").exists()


def test_detach_allows_stash(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    slot = _forward(pm_env, "demo", "foo").resolve()
    git_in_slot(slot, "checkout", "-b", "feature-x")
    (slot / "README.md").write_text("stashable\n")
    git_in_slot(slot, "stash", "push", "-m", "wip")
    detach_mod.detach(pm_env, "demo", repos=None)
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
    pm_env: Paths, marker: str, expected: str,
) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    slot = _forward(pm_env, "demo", "foo").resolve()
    write_git_sentinel(slot, marker)
    with pytest.raises(ProjectError, match=f"{expected} in progress"):
        detach_mod.detach(pm_env, "demo", repos=None)
    assert _forward(pm_env, "demo", "foo").is_symlink()


# --- delete layered on detach ---


def test_delete_per_repo_blocks_on_dirty(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    slot = _forward(pm_env, "demo", "foo").resolve()
    (slot / "README.md").write_text("dirty\n")
    git_in_slot(slot, "add", "README.md")
    with pytest.raises(ProjectError, match="uncommitted changes"):
        delete_mod.delete(pm_env, "demo", repos=["foo"])
    assert _db_rows(pm_env, "demo") == [("foo", _current_uuid(pm_env, "demo", "foo"))]
    assert _forward(pm_env, "demo", "foo").is_symlink()


def test_delete_whole_blocks_on_dirty(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    slot = _forward(pm_env, "demo", "foo").resolve()
    (slot / "README.md").write_text("dirty\n")
    git_in_slot(slot, "add", "README.md")
    with pytest.raises(ProjectError, match="uncommitted changes"):
        delete_mod.delete(pm_env, "demo", repos=None)
    assert (pm_env.projects / "demo" / ".pm.db").is_file()
    assert _forward(pm_env, "demo", "foo").is_symlink()


def test_delete_whole_blocks_on_untracked(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    slot = _forward(pm_env, "demo", "foo").resolve()
    (slot / "scratch.log").write_text("noise\n")
    with pytest.raises(ProjectError, match="untracked"):
        delete_mod.delete(pm_env, "demo", repos=None)
    assert (pm_env.projects / "demo" / ".pm.db").is_file()


def test_delete_whole_blocks_on_in_progress_rebase(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    slot = _forward(pm_env, "demo", "foo").resolve()
    write_git_sentinel(slot, "rebase-merge")
    with pytest.raises(ProjectError, match="rebase in progress"):
        delete_mod.delete(pm_env, "demo", repos=None)
    assert (pm_env.projects / "demo" / ".pm.db").is_file()


# --- dry-run: plan_detach, plan_delete ---


def test_plan_detach_clean_project(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    git_pool(pm_env, "bar", n=1)
    new_mod.new(pm_env, "demo", ["foo", "bar"])
    plan = detach_mod.plan_detach(pm_env, "demo", repos=None)
    assert not plan.has_blocker
    kinds = {a.repo: a.kind for a in plan.actions}
    assert kinds == {"foo": "detach", "bar": "detach"}
    assert all(a.blocker is None for a in plan.actions)
    # unchanged:
    assert _forward(pm_env, "demo", "foo").is_symlink()
    assert _forward(pm_env, "demo", "bar").is_symlink()


def test_plan_detach_reports_blockers_without_mutating(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    git_pool(pm_env, "bar", n=1)
    new_mod.new(pm_env, "demo", ["foo", "bar"])
    foo_slot = _forward(pm_env, "demo", "foo").resolve()
    (foo_slot / "scratch.log").write_text("noise\n")
    bar_slot = _forward(pm_env, "demo", "bar").resolve()
    write_git_sentinel(bar_slot, "CHERRY_PICK_HEAD")
    plan = detach_mod.plan_detach(pm_env, "demo", repos=None)
    assert plan.has_blocker
    by_repo = {a.repo: a for a in plan.actions}
    assert by_repo["foo"].blocker is not None
    assert "untracked" in by_repo["foo"].blocker
    assert by_repo["bar"].blocker is not None
    assert "cherry-pick" in by_repo["bar"].blocker
    assert _forward(pm_env, "demo", "foo").is_symlink()
    assert _forward(pm_env, "demo", "bar").is_symlink()


def test_plan_detach_noop_for_already_detached(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    detach_mod.detach(pm_env, "demo", repos=None)
    plan = detach_mod.plan_detach(pm_env, "demo", repos=None)
    assert not plan.has_blocker
    assert plan.actions[0].kind == "noop"
    assert plan.actions[0].blocker is None


def test_plan_delete_whole_reports_extras(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    (pm_env.projects / "demo" / "notes.txt").write_text("hi")
    plan = delete_mod.plan_delete(pm_env, "demo", repos=None)
    assert plan.has_blocker
    assert any(p.name == "notes.txt" for p in plan.extras)
    # plan still describes the full intent:
    assert plan.remove_readme
    assert plan.drop_db
    assert plan.rmdir
    assert (pm_env.projects / "demo" / "notes.txt").exists()


def test_plan_delete_whole_clean(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    plan = delete_mod.plan_delete(pm_env, "demo", repos=None)
    assert not plan.has_blocker
    assert plan.whole
    assert plan.drop_rows == ["foo"]
    assert plan.remove_readme
    assert plan.drop_db
    assert plan.rmdir
    # nothing mutated
    assert (pm_env.projects / "demo" / ".pm.db").is_file()


def test_plan_delete_per_repo(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    git_pool(pm_env, "bar", n=1)
    new_mod.new(pm_env, "demo", ["foo", "bar"])
    plan = delete_mod.plan_delete(pm_env, "demo", repos=["foo"])
    assert not plan.whole
    assert plan.drop_rows == ["foo"]
    assert not plan.remove_readme
    assert not plan.drop_db
    assert not plan.rmdir
    # bar untouched in plan:
    assert [a.repo for a in plan.detach_plan.actions] == ["foo"]


# --- dry-run CLI exit codes ---


def test_cli_detach_dry_run_exits_zero_when_clean(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    args = argparse.Namespace(
        project="demo", all=True, repos=None, dry_run=True,
    )
    rc = project_cli._cmd_detach(args)
    assert rc == 0
    assert _forward(pm_env, "demo", "foo").is_symlink()


def test_cli_detach_dry_run_exits_one_on_blocker(
    pm_env: Paths, capsys: pytest.CaptureFixture[str],
) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    slot = _forward(pm_env, "demo", "foo").resolve()
    (slot / "scratch.log").write_text("noise\n")
    args = argparse.Namespace(
        project="demo", all=True, repos=None, dry_run=True,
    )
    rc = project_cli._cmd_detach(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "BLOCKED" in err
    assert _forward(pm_env, "demo", "foo").is_symlink()


def test_cli_delete_dry_run_exits_one_on_extras(pm_env: Paths) -> None:
    git_pool(pm_env, "foo", n=1)
    new_mod.new(pm_env, "demo", ["foo"])
    (pm_env.projects / "demo" / "notes.txt").write_text("hi")
    args = argparse.Namespace(
        project="demo", repos=None, dry_run=True,
    )
    rc = project_cli._cmd_delete(args)
    assert rc == 1
    assert (pm_env.projects / "demo" / "notes.txt").exists()
    assert (pm_env.projects / "demo" / ".pm.db").is_file()
