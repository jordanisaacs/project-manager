import subprocess
from pathlib import Path

import pytest

from project_manager.config import Concurrency
from project_manager.paths import Paths
from project_manager.repo import ls as ls_mod
from project_manager.repo import maintenance as maintenance_mod
from project_manager.repo import pull as pull_mod


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


def _commit_file(repo: Path, name: str, content: str = "x\n") -> None:
    (repo / name).write_text(content)
    subprocess.run(["git", "-C", str(repo), "add", name], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-q",
            "-m",
            f"add {name}",
        ],
        check=True,
    )


def _clone_with_upstream(upstream: Path, local: Path, branch: str = "main") -> None:
    subprocess.run(["git", "clone", "-q", str(upstream), str(local)], check=True)
    subprocess.run(
        ["git", "-C", str(local), "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", str(local), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(local), "config", "commit.gpgsign", "false"], check=True)
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
    assert row.fetch_needed is None


def test_repo_ls_detached(pm_env: Paths) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo, branch="main")
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
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


def test_repo_ls_fetch_needed_when_remote_moves(
    pm_env: Paths,
    tmp_path: Path,
) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_repo(upstream, branch="main")
    local = pm_env.repo("foo")
    _clone_with_upstream(upstream, local)

    # Fresh clone: local tracking ref matches remote tip.
    rows = ls_mod.ls(pm_env)
    assert rows[0].fetch_needed is False

    # Upstream moves but we do NOT fetch → remote tip has diverged from @{u}.
    _commit_file(upstream, "remote.txt")
    rows = ls_mod.ls(pm_env)
    assert rows[0].fetch_needed is True

    # After a fetch, @{u} catches up → synced again.
    subprocess.run(["git", "-C", str(local), "fetch", "-q"], check=True)
    rows = ls_mod.ls(pm_env)
    assert rows[0].fetch_needed is False


def test_repo_ls_expected_branch_from_origin_head(
    pm_env: Paths,
    tmp_path: Path,
) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_repo(upstream, branch="neon-main")
    local = pm_env.repo("hadron")
    _clone_with_upstream(upstream, local, branch="neon-main")

    # Origin HEAD points at neon-main; on the trunk → no flag.
    rows = ls_mod.ls(pm_env, check_remote=False)
    assert rows[0].expected_branch == "neon-main"
    assert rows[0].branch == "neon-main"

    # Switch to a feature branch → expected stays neon-main, current differs.
    subprocess.run(
        ["git", "-C", str(local), "checkout", "-q", "-b", "stack/foo"],
        check=True,
    )
    rows = ls_mod.ls(pm_env, check_remote=False)
    assert rows[0].branch == "stack/foo"
    assert rows[0].expected_branch == "neon-main"


def test_repo_ls_expected_branch_from_stacker_config(
    pm_env: Paths,
    tmp_path: Path,
) -> None:
    from project_manager.stacker import config_schema
    from project_manager.stacker.db import StackerDB

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_repo(upstream, branch="main")
    local = pm_env.repo("foo")
    _clone_with_upstream(upstream, local)

    # Stacker config should win over origin/HEAD (here origin/HEAD is main).
    db_path = pm_env.stacker_db()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    StackerDB(db_path).set_config("foo", config_schema.PR_TRUNK, "release")

    rows = ls_mod.ls(pm_env, check_remote=False)
    assert rows[0].expected_branch == "release"


def test_repo_ls_offline_skips_remote_check(
    pm_env: Paths,
    tmp_path: Path,
) -> None:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _init_repo(upstream, branch="main")
    local = pm_env.repo("foo")
    _clone_with_upstream(upstream, local)
    _commit_file(upstream, "remote.txt")

    # With the remote check disabled, fetch_needed stays unknown even though
    # the upstream has moved.
    rows = ls_mod.ls(pm_env, check_remote=False)
    assert rows[0].fetch_needed is None


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
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    results = pull_mod.pull(pm_env, ["foo"])
    assert results[0].ok is False
    assert "dirty" in results[0].message

    head_after = subprocess.run(
        ["git", "-C", str(local), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
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


def _setup_parent_with_submodule(tmp_path: Path, local: Path) -> tuple[Path, Path]:
    """Create sub-upstream, parent-upstream (with sub as submodule), and clone to `local`.

    Returns (parent_upstream, sub_upstream). Upstream dirs are namespaced by
    `local.name` so multiple calls in one test can coexist under the same tmp_path.
    """
    ns = local.name
    sub_upstream = tmp_path / f"sub-upstream-{ns}"
    sub_upstream.mkdir()
    _init_repo(sub_upstream, branch="main")

    parent_upstream = tmp_path / f"parent-upstream-{ns}"
    parent_upstream.mkdir()
    _init_repo(parent_upstream, branch="main")
    subprocess.run(
        [
            "git",
            "-C",
            str(parent_upstream),
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            "-q",
            str(sub_upstream),
            "vendor/sub",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(parent_upstream),
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-q",
            "-m",
            "add submodule",
        ],
        check=True,
    )

    subprocess.run(
        [
            "git",
            "-c",
            "protocol.file.allow=always",
            "clone",
            "-q",
            "--recurse-submodules",
            str(parent_upstream),
            str(local),
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "config", "user.name", "test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "config", "commit.gpgsign", "false"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "config", "protocol.file.allow", "always"],
        check=True,
    )
    subprocess.run(["git", "-C", str(local), "checkout", "-q", "main"], check=True)
    return parent_upstream, sub_upstream


def _bump_submodule_pointer(parent_upstream: Path, sub_upstream: Path) -> None:
    """Advance sub_upstream by one commit and point parent_upstream's gitlink at it."""
    _commit_file(sub_upstream, "sub-new.txt")
    subprocess.run(
        [
            "git",
            "-C",
            str(parent_upstream / "vendor/sub"),
            "-c",
            "protocol.file.allow=always",
            "fetch",
            "-q",
            "origin",
            "main",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(parent_upstream / "vendor/sub"), "checkout", "-q", "FETCH_HEAD"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(parent_upstream), "add", "vendor/sub"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(parent_upstream),
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-q",
            "-m",
            "bump submodule",
        ],
        check=True,
    )


def test_repo_pull_ignores_out_of_date_submodules(pm_env: Paths, tmp_path: Path) -> None:
    """Stale submodule working trees must not block a parent fast-forward.

    When the parent repo's gitlink for a submodule advances (e.g. because
    the user previously pulled without submodule sync), the submodule
    working tree points at the old commit. `git status --porcelain` would
    report that as `M <path>`, falsely classifying the parent as dirty.
    """
    local = pm_env.repo("foo")
    parent_upstream, sub_upstream = _setup_parent_with_submodule(tmp_path, local)

    # Simulate a repo that was previously pulled without submodule sync: bump
    # the gitlink in parent_upstream and fast-forward local manually, leaving
    # the local submodule working tree stale.
    _bump_submodule_pointer(parent_upstream, sub_upstream)
    subprocess.run(
        ["git", "-C", str(local), "fetch", "-q", "origin"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "merge", "-q", "--ff-only", "@{u}"],
        check=True,
    )
    porcelain = subprocess.run(
        ["git", "-C", str(local), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "vendor/sub" in porcelain, "precondition: local starts with stale submodule"

    # Advance parent upstream again so there is something new to pull.
    _commit_file(parent_upstream, "parent-new.txt")

    results = pull_mod.pull(pm_env, ["foo"])
    assert results[0].ok is True, results[0].message
    assert results[0].message == "fast-forwarded"
    assert (local / "parent-new.txt").is_file()


def test_repo_pull_updates_submodules_on_fast_forward(pm_env: Paths, tmp_path: Path) -> None:
    local = pm_env.repo("foo")
    parent_upstream, sub_upstream = _setup_parent_with_submodule(tmp_path, local)
    _bump_submodule_pointer(parent_upstream, sub_upstream)

    results = pull_mod.pull(pm_env, ["foo"])
    assert results[0].ok is True, results[0].message
    assert results[0].message == "fast-forwarded"
    assert (local / "vendor/sub" / "sub-new.txt").is_file()


def test_repo_pull_syncs_submodules_when_parent_up_to_date(pm_env: Paths, tmp_path: Path) -> None:
    """If the parent has nothing new but submodules are stale, pull still syncs them."""
    local = pm_env.repo("foo")
    parent_upstream, sub_upstream = _setup_parent_with_submodule(tmp_path, local)

    # Produce a stale-submodule state without bringing in any new parent commits.
    _bump_submodule_pointer(parent_upstream, sub_upstream)
    subprocess.run(
        ["git", "-C", str(local), "fetch", "-q", "origin"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(local), "merge", "-q", "--ff-only", "@{u}"],
        check=True,
    )
    assert not (local / "vendor/sub" / "sub-new.txt").is_file(), "precondition"

    results = pull_mod.pull(pm_env, ["foo"])
    assert results[0].ok is True, results[0].message
    assert results[0].message == "up-to-date"
    assert (local / "vendor/sub" / "sub-new.txt").is_file()


def test_repo_ls_reports_submodule_state(pm_env: Paths, tmp_path: Path) -> None:
    # Plain repo → no submodules.
    plain = pm_env.repo("plain")
    plain.mkdir()
    _init_repo(plain, branch="main")

    # Repo with an in-sync submodule.
    synced_local = pm_env.repo("synced")
    _setup_parent_with_submodule(tmp_path, synced_local)

    # Repo whose submodule working tree is out of sync with the gitlink.
    stale_local = pm_env.repo("stale")
    stale_parent, stale_sub = _setup_parent_with_submodule(tmp_path, stale_local)
    _bump_submodule_pointer(stale_parent, stale_sub)
    subprocess.run(
        ["git", "-C", str(stale_local), "fetch", "-q", "origin"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(stale_local), "merge", "-q", "--ff-only", "@{u}"],
        check=True,
    )

    rows = {r.repo: r for r in ls_mod.ls(pm_env, check_remote=False)}
    assert rows["plain"].submodules == "none"
    assert rows["synced"].submodules == "synced"
    assert rows["stale"].submodules == "stale"
    # Parent "dirty" stays false — stale submodules are tracked separately.
    assert rows["stale"].dirty is False


def _add_pool_slot(pm_env: Paths, repo: str, uuid: str) -> Path:
    """Create a linked worktree under the pool for `repo`.

    Uses a detached HEAD checkout so the slot never contests the canonical
    repo's branch — same shape as `pool.worktree` builds in production
    code, minus the SQLite ownership row (the maintenance command doesn't
    read the pool db).
    """
    slot_dir = pm_env.pool(repo) / uuid
    slot_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(pm_env.repo(repo)), "worktree", "add", "--detach", str(slot_dir), "HEAD"],
        check=True,
        capture_output=True,
    )
    return slot_dir


def test_repo_maintenance_runs_canonical_and_slot_ops(
    pm_env: Paths,
) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo, branch="main")
    slot_dir = _add_pool_slot(pm_env, "foo", "aaaa")

    results = maintenance_mod.maintain(pm_env, ["foo"], Concurrency())
    ops = {(r.target, r.op) for r in results}
    # Canonical gets maintenance + worktree-prune + update-index; the slot
    # contributes a second update-index row.
    assert ("(repo)", "maintenance") in ops
    assert ("(repo)", "worktree-prune") in ops
    assert ("(repo)", "update-index") in ops
    assert ("aaaa", "update-index") in ops
    assert all(r.ok for r in results), [(r.target, r.op, r.message) for r in results if not r.ok]
    # Sanity: worktree metadata survived the prune call.
    assert (slot_dir / ".git").exists()


def test_repo_maintenance_update_index_tolerates_dirty_tree(
    pm_env: Paths,
) -> None:
    """`update-index --refresh` exits 1 when files need refreshing.

    That's an informational signal (cache primed, some files still dirty),
    not a failure — the timer must not flag it. A touched-but-unchanged
    working-copy file is the simplest way to trigger the nonzero exit.
    """
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo, branch="main")
    (repo / "README.md").write_text((repo / "README.md").read_text())
    # Force the file's mtime forward so --refresh has to re-stat it.
    subprocess.run(["touch", "--date=@100", str(repo / "README.md")], check=True)

    results = maintenance_mod.maintain(pm_env, ["foo"], Concurrency())
    update_rows = [r for r in results if r.op == "update-index"]
    assert update_rows, "expected at least one update-index row"
    assert all(r.ok for r in update_rows)


def test_repo_maintenance_filters_by_repo(pm_env: Paths) -> None:
    for name in ("foo", "bar"):
        d = pm_env.repo(name)
        d.mkdir()
        _init_repo(d, branch="main")

    results = maintenance_mod.maintain(pm_env, ["foo"], Concurrency())
    assert {r.repo for r in results} == {"foo"}


def test_repo_maintenance_all_when_repos_none(pm_env: Paths) -> None:
    for name in ("foo", "bar"):
        d = pm_env.repo(name)
        d.mkdir()
        _init_repo(d, branch="main")

    results = maintenance_mod.maintain(pm_env, None, Concurrency())
    assert {r.repo for r in results} == {"foo", "bar"}


def test_repo_maintenance_unknown_repo_raises(pm_env: Paths) -> None:
    repo = pm_env.repo("foo")
    repo.mkdir()
    _init_repo(repo, branch="main")

    with pytest.raises(ValueError, match="unknown repo"):
        maintenance_mod.maintain(pm_env, ["nope"], Concurrency())
