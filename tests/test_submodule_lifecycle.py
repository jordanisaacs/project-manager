from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import add as pool_add
from project_manager.pool import delete as pool_delete
from project_manager.pool.db import PoolDB
from project_manager.project import add as project_add
from project_manager.project import attach as project_attach
from project_manager.project import detach as project_detach
from project_manager.project import remove as project_remove
from project_manager.stacker import git


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    _run(path, "config", "user.email", "test@example.com")
    _run(path, "config", "user.name", "test")
    _run(path, "config", "protocol.file.allow", "always")
    (path / "README.md").write_text(f"# {path.name}\n")
    _run(path, "add", "README.md")
    _run(path, "commit", "-q", "-m", "initial")


def _commit_file(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content)
    _run(repo, "add", name)
    _run(repo, "commit", "-q", "-m", f"update {name}")
    return _run(repo, "rev-parse", "HEAD").stdout.strip()


def _commit_submodule(repo: Path, path: str, message: str) -> str:
    _run(repo, "add", ".gitmodules", path)
    _run(repo, "commit", "-q", "-m", message)
    return _run(repo, "rev-parse", "HEAD").stdout.strip()


@dataclass(frozen=True)
class NestedRepo:
    repo_name: str
    main_mid: str
    main_leaf: str
    feature_mid: str
    feature_leaf: str
    bump_commit: str


def _nested_repo(pm_env: Paths, tmp_path: Path) -> NestedRepo:
    leaf = tmp_path / "leaf-origin"
    _init_repo(leaf)
    main_leaf = _run(leaf, "rev-parse", "HEAD").stdout.strip()
    feature_leaf = _commit_file(leaf, "leaf-v2.txt", "leaf v2\n")

    mid = tmp_path / "mid-origin"
    _init_repo(mid)
    _run(mid, "submodule", "add", "-q", str(leaf), "deps/leaf")
    _run(mid / "deps/leaf", "checkout", "-q", main_leaf)
    main_mid = _commit_submodule(mid, "deps/leaf", "add nested leaf")
    _run(mid / "deps/leaf", "checkout", "-q", feature_leaf)
    feature_mid = _commit_submodule(mid, "deps/leaf", "bump nested leaf")

    repo_name = "super"
    parent = pm_env.repo(repo_name)
    _init_repo(parent)
    _run(parent, "submodule", "add", "-q", str(mid), "modules/mid")
    _run(parent / "modules/mid", "checkout", "-q", main_mid)
    _run(
        parent / "modules/mid",
        "submodule",
        "update",
        "--init",
        "--recursive",
        "--checkout",
    )
    _commit_submodule(parent, "modules/mid", "add middle submodule")

    _run(parent, "checkout", "-q", "-b", "feature")
    _run(parent / "modules/mid", "checkout", "-q", feature_mid)
    _run(
        parent / "modules/mid",
        "submodule",
        "update",
        "--init",
        "--recursive",
        "--checkout",
    )
    bump_commit = _commit_submodule(parent, "modules/mid", "bump middle submodule")
    _run(parent, "checkout", "-q", "main")
    _run(parent, "submodule", "update", "--init", "--recursive", "--checkout")
    return NestedRepo(
        repo_name=repo_name,
        main_mid=main_mid,
        main_leaf=main_leaf,
        feature_mid=feature_mid,
        feature_leaf=feature_leaf,
        bump_commit=bump_commit,
    )


def _assert_submodules(slot: Path, mid: str, leaf: str) -> None:
    assert git.rev_parse(slot / "modules/mid", "HEAD") == mid
    assert git.rev_parse(slot / "modules/mid/deps/leaf", "HEAD") == leaf
    status = git.git(slot, "status", "--porcelain").stdout
    assert status == ""


def test_project_lifecycle_keeps_changed_nested_submodules_clean(
    pm_env: Paths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    nested = _nested_repo(pm_env, tmp_path)
    slot = pool_add.add(pm_env, nested.repo_name)
    _assert_submodules(slot.path, nested.main_mid, nested.main_leaf)
    # Simulate a free slot left stale by an older/external hard reset. A new
    # project claim repairs it before publishing the forward symlink.
    git.git(slot.path, "reset", "--hard", "feature")
    assert git.git(slot.path, "status", "--porcelain").stdout
    project_add.add(pm_env, "demo", [("app", nested.repo_name)])
    _assert_submodules(slot.path, nested.feature_mid, nested.feature_leaf)

    git.checkout(slot.path, "feature")
    _assert_submodules(slot.path, nested.feature_mid, nested.feature_leaf)

    project_detach.detach(pm_env, "demo", ["app"])
    assert PoolDB(pm_env.pool_db()).is_free(nested.repo_name, slot.uuid)
    _assert_submodules(slot.path, nested.main_mid, nested.main_leaf)

    project_attach.attach(pm_env, "demo", ["app"])
    _assert_submodules(slot.path, nested.feature_mid, nested.feature_leaf)

    project_remove.remove(pm_env, "demo", ["app"])
    assert PoolDB(pm_env.pool_db()).is_free(nested.repo_name, slot.uuid)
    _assert_submodules(slot.path, nested.main_mid, nested.main_leaf)

    project_add.add(pm_env, "demo", [("app", nested.repo_name)])
    git.checkout(slot.path, "feature")
    project_remove.remove(pm_env, "demo", None)
    assert not pm_env.project("demo").exists()
    assert PoolDB(pm_env.pool_db()).is_free(nested.repo_name, slot.uuid)
    _assert_submodules(slot.path, nested.main_mid, nested.main_leaf)
    pool_delete.delete(pm_env, nested.repo_name, slot.uuid)
    assert not slot.path.exists()


def test_stacker_reset_and_cherry_pick_update_nested_submodules(
    pm_env: Paths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    nested = _nested_repo(pm_env, tmp_path)
    slot = pool_add.add(pm_env, nested.repo_name)
    git.checkout(slot.path, "-b", "work", "main")

    git.reset_hard(slot.path, "feature")
    _assert_submodules(slot.path, nested.feature_mid, nested.feature_leaf)
    git.reset_hard(slot.path, "main")
    _assert_submodules(slot.path, nested.main_mid, nested.main_leaf)

    picked = git.cherry_pick(slot.path, nested.bump_commit)
    assert picked.returncode == 0, picked.stderr
    _assert_submodules(slot.path, nested.feature_mid, nested.feature_leaf)
