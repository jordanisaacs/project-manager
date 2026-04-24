import subprocess
from pathlib import Path

import pytest

from project_manager.errors import CommandError
from project_manager.paths import Paths
from project_manager.pool import add as add_mod
from project_manager.pool import ls as ls_mod
from project_manager.pool import worktree as wt_mod
from project_manager.project import create as create_mod


def _init_repo(path: Path, branch: str = "main") -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "commit.gpgsign", "false"], check=True
    )
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

    create_mod.create(pm_env, "demo", [("foo", "foo")])
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
