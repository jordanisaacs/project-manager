from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from project_manager.errors import CommandError
from project_manager.paths import Paths
from project_manager.pool import add as pool_add
from project_manager.stacker import git


@pytest.fixture(autouse=True)
def _allow_file_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")


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
    (path / "tracked.txt").write_text(f"{path.name} v1\n")
    _run(path, "add", "tracked.txt")
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
class ParentRepo:
    name: str
    path: Path
    submodule_origin: Path
    submodule_path: str
    submodule_commit: str


def _parent_repo(pm_env: Paths, tmp_path: Path) -> ParentRepo:
    origin = tmp_path / "submodule-origin"
    _init_repo(origin)
    commit = _run(origin, "rev-parse", "HEAD").stdout.strip()

    name = "super"
    parent = pm_env.repo(name)
    _init_repo(parent)
    submodule_path = "vendor/sub"
    _run(parent, "submodule", "add", "-q", str(origin), submodule_path)
    _commit_submodule(parent, submodule_path, "add submodule")
    return ParentRepo(
        name=name,
        path=parent,
        submodule_origin=origin,
        submodule_path=submodule_path,
        submodule_commit=commit,
    )


def _remove_canonical_submodule(parent: ParentRepo, *, keep_object_store: bool) -> Path:
    checkout = parent.path / parent.submodule_path
    git_dir = Path(_run(checkout, "rev-parse", "--absolute-git-dir").stdout.strip())
    _run(parent.path, "submodule", "deinit", "-f", "--", parent.submodule_path)
    if not keep_object_store:
        shutil.rmtree(git_dir)
    return git_dir


def _origin(repo: Path) -> str:
    return _run(repo, "remote", "get-url", "origin").stdout.strip()


def _assert_initialized(slot: Path, parent: ParentRepo, commit: str | None = None) -> Path:
    submodule = slot / parent.submodule_path
    assert git.rev_parse(submodule, "HEAD") == (commit or parent.submodule_commit)
    assert _origin(submodule) == str(parent.submodule_origin)
    assert git.git(slot, "status", "--porcelain").stdout == ""
    assert not (submodule / ".git" / "objects" / "info" / "alternates").exists()
    return submodule


def test_pool_add_reuses_canonical_submodule_object_store_when_remote_is_unavailable(
    pm_env: Paths,
    tmp_path: Path,
) -> None:
    parent = _parent_repo(pm_env, tmp_path)
    object_store = _remove_canonical_submodule(parent, keep_object_store=True)
    parent.submodule_origin.rename(tmp_path / "offline-origin")

    slot = pool_add.add(pm_env, parent.name)

    assert object_store.is_dir()
    _assert_initialized(slot.path, parent)


def test_pool_add_reuses_dirty_pool_submodule_without_copying_uncommitted_state(
    pm_env: Paths,
    tmp_path: Path,
) -> None:
    parent = _parent_repo(pm_env, tmp_path)
    source_slot = pool_add.add(pm_env, parent.name)
    _remove_canonical_submodule(parent, keep_object_store=False)
    parent.submodule_origin.rename(tmp_path / "offline-origin")

    source = source_slot.path / parent.submodule_path
    (source / "tracked.txt").write_text("dirty source content\n")
    (source / "untracked.txt").write_text("source-only\n")
    _run(source, "add", "tracked.txt")

    new_slot = pool_add.add(pm_env, parent.name)

    initialized = _assert_initialized(new_slot.path, parent)
    assert initialized.joinpath("tracked.txt").read_text() == "submodule-origin v1\n"
    assert not initialized.joinpath("untracked.txt").exists()


def test_pool_add_falls_back_to_remote_when_local_store_lacks_required_commit(
    pm_env: Paths,
    tmp_path: Path,
) -> None:
    parent = _parent_repo(pm_env, tmp_path)
    new_commit = _commit_file(parent.submodule_origin, "v2.txt", "remote v2\n")

    # The canonical checkout/object store still has only v1. Point the parent
    # at v2 without fetching it locally so the source is URL-compatible but
    # cannot satisfy the exact gitlink.
    _run(
        parent.path,
        "update-index",
        "--cacheinfo",
        "160000",
        new_commit,
        parent.submodule_path,
    )
    _run(parent.path, "commit", "-q", "-m", "bump submodule")

    slot = pool_add.add(pm_env, parent.name)

    initialized = _assert_initialized(slot.path, parent, new_commit)
    assert initialized.joinpath("v2.txt").read_text() == "remote v2\n"


def test_pool_add_rejects_mismatched_local_source_and_cleans_failed_slot(
    pm_env: Paths,
    tmp_path: Path,
) -> None:
    parent = _parent_repo(pm_env, tmp_path)
    canonical = parent.path / parent.submodule_path
    _run(canonical, "remote", "set-url", "origin", str(tmp_path / "different-origin"))
    parent.submodule_origin.rename(tmp_path / "offline-origin")
    registrations_before = _run(parent.path, "worktree", "list", "--porcelain").stdout

    with pytest.raises(CommandError):
        pool_add.add(pm_env, parent.name)

    assert _run(parent.path, "worktree", "list", "--porcelain").stdout == registrations_before
    assert list(pm_env.pool(parent.name).iterdir()) == []


@dataclass(frozen=True)
class NestedRepo:
    name: str
    parent: Path
    middle_origin: Path
    leaf_origin: Path
    middle_commit: str
    leaf_commit: str


def _nested_repo(pm_env: Paths, tmp_path: Path) -> NestedRepo:
    leaf = tmp_path / "leaf-origin"
    _init_repo(leaf)
    leaf_commit = _run(leaf, "rev-parse", "HEAD").stdout.strip()

    middle = tmp_path / "middle-origin"
    _init_repo(middle)
    _run(middle, "submodule", "add", "-q", str(leaf), "deps/leaf")
    middle_commit = _commit_submodule(middle, "deps/leaf", "add leaf")

    name = "nested-super"
    parent = pm_env.repo(name)
    _init_repo(parent)
    _run(parent, "submodule", "add", "-q", str(middle), "modules/middle")
    _run(parent / "modules/middle", "submodule", "update", "--init", "--checkout")
    _commit_submodule(parent, "modules/middle", "add middle")
    return NestedRepo(
        name=name,
        parent=parent,
        middle_origin=middle,
        leaf_origin=leaf,
        middle_commit=middle_commit,
        leaf_commit=leaf_commit,
    )


def test_pool_add_reuses_existing_slot_recursively_when_all_remotes_are_unavailable(
    pm_env: Paths,
    tmp_path: Path,
) -> None:
    nested = _nested_repo(pm_env, tmp_path)
    source_slot = pool_add.add(pm_env, nested.name)

    canonical_middle = nested.parent / "modules/middle"
    canonical_git_dir = Path(
        _run(canonical_middle, "rev-parse", "--absolute-git-dir").stdout.strip()
    )
    _run(nested.parent, "submodule", "deinit", "-f", "--", "modules/middle")
    shutil.rmtree(canonical_git_dir)
    nested.middle_origin.rename(tmp_path / "offline-middle")
    nested.leaf_origin.rename(tmp_path / "offline-leaf")

    new_slot = pool_add.add(pm_env, nested.name)

    middle = new_slot.path / "modules/middle"
    leaf = middle / "deps/leaf"
    assert git.rev_parse(middle, "HEAD") == nested.middle_commit
    assert git.rev_parse(leaf, "HEAD") == nested.leaf_commit
    assert _origin(middle) == str(nested.middle_origin)
    assert _origin(leaf) == str(nested.leaf_origin)
    assert git.git(new_slot.path, "status", "--porcelain").stdout == ""
    assert source_slot.path.is_dir()
