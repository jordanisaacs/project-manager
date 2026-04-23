import subprocess
from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.repo import ls as ls_mod
from project_manager.repo import pull as pull_mod


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
        ["git", "-C", str(path), "-c", "core.hooksPath=/dev/null",
         "commit", "-q", "-m", "init"],
        check=True,
    )


def _commit_file(repo: Path, name: str, content: str = "x\n") -> None:
    (repo / name).write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", name], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "core.hooksPath=/dev/null",
         "commit", "-q", "-m", f"add {name}"],
        check=True,
    )


def _clone_with_upstream(upstream: Path, local: Path, branch: str = "main") -> None:
    subprocess.run(["git", "clone", "-q", str(upstream), str(local)], check=True)
    subprocess.run(
        ["git", "-C", str(local), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(local), "config", "user.name", "test"], check=True)
    subprocess.run(
        ["git", "-C", str(local), "config", "commit.gpgsign", "false"], check=True
    )
    subprocess.run(["git", "-C", str(local), "checkout", "-q", branch], check=True)


def test_repo_ls_clean_on_branch(pm_env: Paths) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo, branch="main")

    rows = ls_mod.ls(pm_env)
    assert len(rows) == 1
    row = rows[0]
    assert row.repo == "foo"
    assert row.branch == "main"
    assert row.dirty is False
    assert row.has_upstream is False


def test_repo_ls_detached(pm_env: Paths) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo, branch="main")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", sha], check=True)

    rows = ls_mod.ls(pm_env)
    assert rows[0].branch is None


def test_repo_ls_dirty(pm_env: Paths) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo)
    (repo / "new.txt").write_text("hi\n")

    rows = ls_mod.ls(pm_env)
    assert rows[0].dirty is True


def test_repo_ls_ahead_behind(pm_env: Paths, tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_repo(upstream, branch="main")

    local = pm_env.repo("foo")
    _clone_with_upstream(upstream, local)

    # local commit → ahead 1 behind 0
    _commit_file(local, "local.txt")
    rows = ls_mod.ls(pm_env)
    assert rows[0].has_upstream is True
    assert rows[0].ahead == 1
    assert rows[0].behind == 0

    # upstream commit + fetch → ahead 1 behind 1
    _commit_file(upstream, "remote.txt")
    subprocess.run(["git", "-C", str(local), "fetch", "-q"], check=True)
    rows = ls_mod.ls(pm_env)
    assert rows[0].ahead == 1
    assert rows[0].behind == 1


def test_repo_pull_fast_forwards(pm_env: Paths, tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_repo(upstream, branch="main")

    local = pm_env.repo("foo")
    _clone_with_upstream(upstream, local)
    _commit_file(upstream, "remote.txt")

    results = pull_mod.pull(pm_env, ["foo"])
    assert len(results) == 1
    assert results[0].repo == "foo"
    assert results[0].ok is True
    assert results[0].message == "fast-forwarded"
    assert (local / "remote.txt").is_file()


def test_repo_pull_skips_dirty(pm_env: Paths, tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_repo(upstream, branch="main")

    local = pm_env.repo("foo")
    _clone_with_upstream(upstream, local)
    _commit_file(upstream, "remote.txt")
    (local / "dirt.txt").write_text("dirt\n")

    head_before = subprocess.run(
        ["git", "-C", str(local), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    results = pull_mod.pull(pm_env, ["foo"])
    assert results[0].ok is False
    assert "dirty" in results[0].message

    head_after = subprocess.run(
        ["git", "-C", str(local), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head_before == head_after


def test_repo_pull_up_to_date(pm_env: Paths, tmp_path: Path) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_repo(upstream, branch="main")

    local = pm_env.repo("foo")
    _clone_with_upstream(upstream, local)

    results = pull_mod.pull(pm_env, ["foo"])
    assert results[0].ok is True
    assert results[0].message == "up-to-date"


def test_repo_pull_filters_by_repo(pm_env: Paths, tmp_path: Path) -> None:
    up1 = tmp_path / "up1"
    up1.mkdir()
    _init_repo(up1, branch="main")
    up2 = tmp_path / "up2"
    up2.mkdir()
    _init_repo(up2, branch="main")

    _clone_with_upstream(up1, pm_env.repo("foo"))
    _clone_with_upstream(up2, pm_env.repo("bar"))

    results = pull_mod.pull(pm_env, ["foo"])
    assert [r.repo for r in results] == ["foo"]


def test_repo_pull_all_when_repos_none(pm_env: Paths, tmp_path: Path) -> None:
    up1 = tmp_path / "up1"
    up1.mkdir()
    _init_repo(up1, branch="main")
    up2 = tmp_path / "up2"
    up2.mkdir()
    _init_repo(up2, branch="main")

    _clone_with_upstream(up1, pm_env.repo("foo"))
    _clone_with_upstream(up2, pm_env.repo("bar"))

    results = pull_mod.pull(pm_env, None)
    assert sorted(r.repo for r in results) == ["bar", "foo"]


def test_repo_pull_unknown_repo_raises(pm_env: Paths) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo, branch="main")

    with pytest.raises(ValueError, match="unknown repo"):
        pull_mod.pull(pm_env, ["nope"])


def test_repo_pull_no_upstream_skipped(pm_env: Paths) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo, branch="main")

    results = pull_mod.pull(pm_env, ["foo"])
    assert results[0].ok is False
    assert "no upstream" in results[0].message
