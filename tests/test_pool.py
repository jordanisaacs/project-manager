import json
import subprocess
from pathlib import Path

import pytest

import project_manager.cli  # noqa: F401  — register CLI apps before importing a leaf command
from project_manager.errors import CommandError
from project_manager.paths import Paths
from project_manager.pool import add as add_mod
from project_manager.pool import delete as delete_mod
from project_manager.pool import ls as ls_mod
from project_manager.pool import worktree as wt_mod
from project_manager.pool.cli.delete import delete as cli_pool_delete
from project_manager.pool.db import OWNER_STACKER_OPS, PoolDB
from project_manager.project import add as project_add_mod
from tests.helpers import write_git_sentinel


def _init_repo(path: Path, branch: str = "main") -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "commit.gpgsign", "false"], check=True)
    (path / "README.md").write_text("# test\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "init"],
        check=True,
    )


def test_default_branch_from_current(pm_env: Paths) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo, branch="main")
    assert wt_mod.default_branch(repo) == "main"


def test_pool_add_creates_detached_worktree(pm_env: Paths) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo, branch="main")

    slot = add_mod.add(pm_env, "foo")
    assert slot.path.is_dir()
    assert (slot.path / ".git").exists()
    assert (slot.path / "README.md").is_file()

    # HEAD is detached
    result = subprocess.run(
        ["git", "-C", str(slot.path), "symbolic-ref", "-q", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0  # detached


def test_pool_ls_free_and_claimed(pm_env: Paths) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo, branch="main")
    add_mod.add(pm_env, "foo")
    add_mod.add(pm_env, "foo")

    rows_before = ls_mod.ls(pm_env, "foo")
    assert len(rows_before) == 2
    assert all(r.status == "FREE" for r in rows_before)
    # Fresh pool slots are checked out in detached HEAD state.
    assert all(r.branch == "(detached)" for r in rows_before)

    project_add_mod.add(pm_env, "demo", [("foo", "foo")])
    rows_after = ls_mod.ls(pm_env, "foo")
    statuses = sorted(r.status for r in rows_after)
    assert statuses == ["FREE", "demo"]
    claimed = next(r for r in rows_after if r.status == "demo")
    # Slot stays on whatever HEAD the pool started with (detached for a
    # fresh pool slot); attach doesn't force a branch checkout.
    assert claimed.branch == "(detached)"


def test_pool_ls_all_repos(pm_env: Paths) -> None:
    for name in ["aa", "bb"]:
        repo = pm_env.repo(name)
        repo.mkdir()
        _init_repo(repo)
        add_mod.add(pm_env, name)

    rows = ls_mod.ls(pm_env, None)
    repos = sorted({r.repo for r in rows})
    assert repos == ["aa", "bb"]


def test_pool_add_copies_untracked_claude_md(pm_env: Paths) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo)
    (repo / "CLAUDE.md").write_text("# guidance\n")  # untracked

    slot = add_mod.add(pm_env, "foo")
    assert (slot.path / "CLAUDE.md").read_text() == "# guidance\n"


def test_pool_add_errors_on_missing_repo(pm_env: Paths) -> None:
    with pytest.raises(CommandError):
        add_mod.add(pm_env, "nonexistent")


def test_pool_delete_removes_path_and_git_registration(pm_env: Paths) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo)
    slot = add_mod.add(pm_env, "foo")

    deleted = delete_mod.delete(pm_env, "foo", slot.uuid)

    assert deleted.path == slot.path
    assert not slot.path.exists()
    assert PoolDB(pm_env.pool_db()).get_owner("foo", slot.uuid) is None
    registered = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert str(slot.path) not in registered


def test_pool_delete_dry_run_json_is_non_mutating(
    pm_env: Paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo)
    slot = add_mod.add(pm_env, "foo")

    assert cli_pool_delete("foo", slot.uuid, dry_run=True, json=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "repo": "foo",
        "uuid": slot.uuid,
        "path": str(slot.path),
        "branch": None,
        "action": "delete",
        "safe": True,
        "blocker": None,
    }
    assert slot.path.is_dir()


def test_pool_delete_json_reports_deleted_slot(
    pm_env: Paths,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo)
    slot = add_mod.add(pm_env, "foo")

    assert cli_pool_delete("foo", slot.uuid, json=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == [
        {
            "repo": "foo",
            "uuid": slot.uuid,
            "path": str(slot.path),
            "status": "deleted",
        }
    ]
    assert not slot.path.exists()


def test_pool_delete_refuses_project_claim(pm_env: Paths) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo)
    slot = add_mod.add(pm_env, "foo")
    project_add_mod.add(pm_env, "demo", [("foo", "foo")])

    plan = delete_mod.plan_delete(pm_env, "foo", slot.uuid)
    assert plan.has_blocker
    assert plan.blocker == "slot is claimed by project 'demo'"
    with pytest.raises(CommandError, match="claimed by project 'demo'"):
        delete_mod.delete(pm_env, "foo", slot.uuid)
    assert slot.path.is_dir()


def test_pool_delete_refuses_attached_link_when_ownership_drifted(pm_env: Paths) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo)
    slot = add_mod.add(pm_env, "foo")
    project_add_mod.add(pm_env, "demo", [("foo", "foo")])
    PoolDB(pm_env.pool_db()).release("foo", slot.uuid)

    plan = delete_mod.plan_delete(pm_env, "foo", slot.uuid)

    assert plan.has_blocker
    assert plan.blocker is not None
    assert "still attached" in plan.blocker
    with pytest.raises(CommandError, match="still attached"):
        delete_mod.delete(pm_env, "foo", slot.uuid)
    assert slot.path.is_dir()


def test_pool_delete_refuses_stacker_claim(pm_env: Paths) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo)
    slot = add_mod.add(pm_env, "foo")
    PoolDB(pm_env.pool_db()).claim("foo", slot.uuid, OWNER_STACKER_OPS)

    with pytest.raises(CommandError, match="stacker operation 'ops'"):
        delete_mod.delete(pm_env, "foo", slot.uuid)
    assert slot.path.is_dir()


@pytest.mark.parametrize("unsafe_state", ["tracked", "untracked", "operation"])
def test_pool_delete_refuses_unsafe_worktree_state(
    pm_env: Paths,
    unsafe_state: str,
) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo)
    slot = add_mod.add(pm_env, "foo")
    if unsafe_state == "tracked":
        (slot.path / "README.md").write_text("changed\n")
    elif unsafe_state == "untracked":
        (slot.path / "scratch.txt").write_text("untracked\n")
    else:
        write_git_sentinel(slot.path, "CHERRY_PICK_HEAD")

    plan = delete_mod.plan_delete(pm_env, "foo", slot.uuid)

    assert plan.has_blocker
    with pytest.raises(CommandError, match="refusing to delete"):
        delete_mod.delete(pm_env, "foo", slot.uuid)
    assert slot.path.is_dir()


def test_pool_delete_refuses_unreachable_detached_commit(pm_env: Paths) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo)
    slot = add_mod.add(pm_env, "foo")
    (slot.path / "only-here.txt").write_text("keep me\n")
    subprocess.run(["git", "-C", str(slot.path), "add", "only-here.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(slot.path),
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-q",
            "-m",
            "detached work",
        ],
        check=True,
    )

    plan = delete_mod.plan_delete(pm_env, "foo", slot.uuid)

    assert plan.has_blocker
    assert plan.blocker is not None
    assert "not reachable" in plan.blocker
    assert slot.path.is_dir()


def test_pool_delete_refuses_locked_worktree(pm_env: Paths) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo)
    slot = add_mod.add(pm_env, "foo")
    subprocess.run(["git", "-C", str(repo), "worktree", "lock", str(slot.path)], check=True)
    try:
        plan = delete_mod.plan_delete(pm_env, "foo", slot.uuid)
        assert plan.blocker == "Git worktree is locked"
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "unlock", str(slot.path)],
            check=True,
        )


def test_pool_delete_rejects_non_exact_slot_component(pm_env: Paths) -> None:
    with pytest.raises(CommandError, match="one exact path component"):
        delete_mod.plan_delete(pm_env, "foo", "../other")
