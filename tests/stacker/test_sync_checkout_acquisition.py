from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OWNER_STACKER_OPS, Owner, OwnerKind, PoolDB
from project_manager.project import add as project_add
from project_manager.project import lease as project_lease
from project_manager.stacker import git, ops_slot
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import (
    OperationState,
    ParentLocator,
    ScopeSpec,
    SelectorTarget,
    SyncOptions,
    WorktreeInit,
)
from project_manager.stacker.service import StackerService

from .conftest import commit_file


def _service(paths: Paths, notices: list[str]) -> StackerService:
    return StackerService(StackerDB(paths.stacker_db()), paths, progress=notices.append)


def _initialize(
    service: StackerService,
    repo_name: str,
    slot: slot_mod.Slot,
    branch: str,
    parent: str = "main",
) -> None:
    service.init_new_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=slot.path,
            branch=branch,
            parent=ParentLocator(repo_name=repo_name, branch=parent),
        )
    )


def _claim_project(pooldb: PoolDB, slot: slot_mod.Slot, project: str) -> None:
    pooldb.claim(slot.repo, slot.uuid, Owner(OwnerKind.PROJECT, project))


def _advance_main(repo_path: Path, *, conflict: bool = False) -> str:
    path = "shared.txt" if conflict else "parent.txt"
    content = "main\n" if conflict else "parent\n"
    return commit_file(repo_path, path, content, "advance main")


def test_sync_reuses_current_pm_checkout_before_waiting_for_downstream_branch(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A later queue branch in cwd is a safe workspace for the whole sync.

    Before the fix, resolving ``parent`` skipped cwd because it was on the
    live ``leaf`` branch, entered ``ops_slot.acquire``, and waited for the
    unrelated stacker-owned slot instead of using the project-owned checkout.
    """
    repo_name, repo_path = stacker_repo
    parent_slot, current_slot, held_slot = three_slots
    notices: list[str] = []
    service = _service(pm_env, notices)
    pooldb = PoolDB(pm_env.pool_db())

    _initialize(service, repo_name, parent_slot, "parent")
    commit_file(parent_slot.path, "parent-feature.txt", "parent\n", "parent work")
    _initialize(service, repo_name, current_slot, "leaf", parent="parent")
    commit_file(current_slot.path, "leaf.txt", "leaf\n", "leaf work")
    git.checkout(parent_slot.path, "-b", "parking", "main")

    _claim_project(pooldb, parent_slot, "other-project")
    _claim_project(pooldb, current_slot, "current-project")
    pooldb.claim(held_slot.repo, held_slot.uuid, OWNER_STACKER_OPS)
    monkeypatch.chdir(current_slot.path)
    _advance_main(repo_path)

    def fail_if_pool_acquired(*_args: object, **_kwargs: object) -> slot_mod.Slot:
        pytest.fail("sync entered the pool wait/acquire path despite a safe cwd checkout")

    monkeypatch.setattr(ops_slot, "acquire", fail_if_pool_acquired)

    result = service.sync(
        SelectorTarget(repo_name, "leaf"),
        options=SyncOptions(offline=True),
    )

    assert "Sync complete." in result
    assert not any("Waiting for a stacker slot" in notice for notice in notices)
    assert git.current_branch(current_slot.path) == "leaf"
    assert pooldb.get_owner(repo_name, current_slot.uuid) == Owner(
        OwnerKind.PROJECT, "current-project"
    )
    assert pooldb.get_owner(repo_name, held_slot.uuid) == OWNER_STACKER_OPS


def test_sync_reuses_exact_branch_in_another_pm_checkout_before_waiting(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, repo_path = stacker_repo
    target_slot, cwd_slot, held_slot = three_slots
    notices: list[str] = []
    service = _service(pm_env, notices)
    pooldb = PoolDB(pm_env.pool_db())

    _initialize(service, repo_name, target_slot, "feature")
    commit_file(target_slot.path, "feature.txt", "feature\n", "feature work")
    git.checkout(cwd_slot.path, "-b", "unrelated", "main")
    _claim_project(pooldb, target_slot, "target-project")
    _claim_project(pooldb, cwd_slot, "cwd-project")
    pooldb.claim(held_slot.repo, held_slot.uuid, OWNER_STACKER_OPS)
    monkeypatch.chdir(cwd_slot.path)
    new_main = _advance_main(repo_path)

    def fail_if_pool_acquired(*_args: object, **_kwargs: object) -> slot_mod.Slot:
        pytest.fail("sync ignored an exact safe checkout and entered the pool wait path")

    monkeypatch.setattr(ops_slot, "acquire", fail_if_pool_acquired)

    result = service.sync(
        SelectorTarget(repo_name, "feature"),
        ScopeSpec(only=True),
        options=SyncOptions(offline=True),
    )

    assert "Sync complete." in result
    assert not any("Waiting for a stacker slot" in notice for notice in notices)
    assert git.current_branch(cwd_slot.path) == "unrelated"
    assert git.rev_parse(target_slot.path, "HEAD~1") == new_main
    assert pooldb.get_owner(repo_name, target_slot.uuid) == Owner(
        OwnerKind.PROJECT, "target-project"
    )


def test_sync_reuses_exact_current_pm_checkout_before_waiting(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, repo_path = stacker_repo
    current_slot, other_slot, held_slot = three_slots
    notices: list[str] = []
    service = _service(pm_env, notices)
    pooldb = PoolDB(pm_env.pool_db())

    _initialize(service, repo_name, current_slot, "feature")
    commit_file(current_slot.path, "feature.txt", "feature\n", "feature work")
    _claim_project(pooldb, current_slot, "current-project")
    _claim_project(pooldb, other_slot, "other-project")
    pooldb.claim(held_slot.repo, held_slot.uuid, OWNER_STACKER_OPS)
    monkeypatch.chdir(current_slot.path)
    _advance_main(repo_path)

    def fail_if_pool_acquired(*_args: object, **_kwargs: object) -> slot_mod.Slot:
        pytest.fail("sync ignored the exact current checkout and entered the pool wait path")

    monkeypatch.setattr(ops_slot, "acquire", fail_if_pool_acquired)

    result = service.sync(
        SelectorTarget(repo_name, "feature"),
        ScopeSpec(only=True),
        options=SyncOptions(offline=True),
    )

    assert "Sync complete." in result
    assert not any("Waiting for a stacker slot" in notice for notice in notices)
    assert git.current_branch(current_slot.path) == "feature"
    assert pooldb.get_owner(repo_name, current_slot.uuid) == Owner(
        OwnerKind.PROJECT, "current-project"
    )


def test_sync_reuse_preserves_project_owner_and_topology_lease(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, repo_path = stacker_repo
    notices: list[str] = []
    service = _service(pm_env, notices)
    [added] = project_add.add(pm_env, "leased-project", [("checkout", repo_name)])
    target_slot = next(slot for slot in three_slots if slot.uuid == added.uuid)
    pooldb = PoolDB(pm_env.pool_db())

    _initialize(service, repo_name, target_slot, "feature")
    commit_file(target_slot.path, "feature.txt", "feature\n", "feature work")
    lease = project_lease.acquire(
        pm_env,
        "leased-project",
        "test:holder",
        "session-1",
    )
    for index, slot in enumerate(slot for slot in three_slots if slot != target_slot):
        if index == 0:
            _claim_project(pooldb, slot, "other-project")
        else:
            pooldb.claim(slot.repo, slot.uuid, OWNER_STACKER_OPS)
    monkeypatch.chdir(target_slot.path)
    _advance_main(repo_path)

    result = service.sync(
        SelectorTarget(repo_name, "feature"),
        ScopeSpec(only=True),
        options=SyncOptions(offline=True),
    )

    assert "Sync complete." in result
    assert pooldb.get_owner(repo_name, target_slot.uuid) == Owner(
        OwnerKind.PROJECT, "leased-project"
    )
    assert project_lease.list_for_projects(pm_env, ["leased-project"]) == [lease]


def test_sync_does_not_reuse_dirty_exact_pm_checkout(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, repo_path = stacker_repo
    target_slot, other_slot, held_slot = three_slots
    notices: list[str] = []
    service = _service(pm_env, notices)
    pooldb = PoolDB(pm_env.pool_db())

    _initialize(service, repo_name, target_slot, "feature")
    original_head = commit_file(target_slot.path, "feature.txt", "feature\n", "feature work")
    (target_slot.path / "untracked.txt").write_text("do not overwrite\n")
    _claim_project(pooldb, target_slot, "target-project")
    _claim_project(pooldb, other_slot, "other-project")
    pooldb.claim(held_slot.repo, held_slot.uuid, OWNER_STACKER_OPS)
    monkeypatch.chdir(target_slot.path)
    _advance_main(repo_path)

    with pytest.raises(git.GitError, match=r"dirty|untracked"):
        service.sync(
            SelectorTarget(repo_name, "feature"),
            ScopeSpec(only=True),
            options=SyncOptions(offline=True),
        )

    assert git.rev_parse(target_slot.path, "HEAD") == original_head
    assert (target_slot.path / "untracked.txt").read_text() == "do not overwrite\n"
    assert service.db.get_operation(repo_name) is None
    assert pooldb.get_owner(repo_name, target_slot.uuid) == Owner(
        OwnerKind.PROJECT, "target-project"
    )


def test_sync_does_not_reuse_checkout_with_git_operation_in_progress(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, repo_path = stacker_repo
    target_slot = three_slots[0]
    notices: list[str] = []
    service = _service(pm_env, notices)
    pooldb = PoolDB(pm_env.pool_db())

    _initialize(service, repo_name, target_slot, "feature")
    original_head = commit_file(target_slot.path, "feature.txt", "feature\n", "feature work")
    _claim_project(pooldb, target_slot, "target-project")
    monkeypatch.chdir(target_slot.path)
    _advance_main(repo_path)

    real_in_progress = git.in_progress_operation

    def in_progress(path: Path) -> str | None:
        if path.resolve() == target_slot.path.resolve():
            return "merge"
        return real_in_progress(path)

    monkeypatch.setattr(git, "in_progress_operation", in_progress)

    with pytest.raises(git.GitError, match="merge is in progress"):
        service.sync(
            SelectorTarget(repo_name, "feature"),
            ScopeSpec(only=True),
            options=SyncOptions(offline=True),
        )

    assert git.rev_parse(target_slot.path, "HEAD") == original_head
    assert service.db.get_operation(repo_name) is None
    assert pooldb.get_owner(repo_name, target_slot.uuid) == Owner(
        OwnerKind.PROJECT, "target-project"
    )


def test_sync_does_not_borrow_dirty_current_checkout_for_downstream_branch(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, repo_path = stacker_repo
    parent_slot, current_slot, held_slot = three_slots
    notices: list[str] = []
    service = _service(pm_env, notices)
    pooldb = PoolDB(pm_env.pool_db())

    _initialize(service, repo_name, parent_slot, "parent")
    commit_file(parent_slot.path, "parent-feature.txt", "parent\n", "parent work")
    _initialize(service, repo_name, current_slot, "leaf", parent="parent")
    commit_file(current_slot.path, "leaf.txt", "leaf\n", "leaf work")
    git.checkout(parent_slot.path, "-b", "parking", "main")
    (current_slot.path / "untracked.txt").write_text("keep me\n")
    _claim_project(pooldb, parent_slot, "other-project")
    _claim_project(pooldb, current_slot, "current-project")
    pooldb.claim(held_slot.repo, held_slot.uuid, OWNER_STACKER_OPS)
    monkeypatch.chdir(current_slot.path)
    _advance_main(repo_path)

    def would_wait(*_args: object, **_kwargs: object) -> slot_mod.Slot:
        raise slot_mod.PoolExhaustedError("unsafe cwd requires a pool slot")

    monkeypatch.setattr(ops_slot, "acquire", would_wait)

    with pytest.raises(slot_mod.PoolExhaustedError, match="unsafe cwd"):
        service.sync(
            SelectorTarget(repo_name, "leaf"),
            options=SyncOptions(offline=True),
        )

    assert git.current_branch(current_slot.path) == "leaf"
    assert (current_slot.path / "untracked.txt").read_text() == "keep me\n"
    assert service.db.get_operation(repo_name) is None
    assert pooldb.get_owner(repo_name, current_slot.uuid) == Owner(
        OwnerKind.PROJECT, "current-project"
    )


def test_sync_does_not_reuse_exact_checkout_reserved_by_another_stacker_op(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_name, repo_path = stacker_repo
    reserved_slot, project_slot_a, project_slot_b = three_slots
    notices: list[str] = []
    service = _service(pm_env, notices)
    pooldb = PoolDB(pm_env.pool_db())

    _initialize(service, repo_name, reserved_slot, "feature")
    original_head = commit_file(
        reserved_slot.path,
        "feature.txt",
        "feature\n",
        "feature work",
    )
    pooldb.claim(reserved_slot.repo, reserved_slot.uuid, OWNER_STACKER_OPS)
    _claim_project(pooldb, project_slot_a, "project-a")
    _claim_project(pooldb, project_slot_b, "project-b")
    monkeypatch.chdir(tmp_path)
    _advance_main(repo_path)

    def would_wait(*_args: object, **_kwargs: object) -> slot_mod.Slot:
        raise slot_mod.PoolExhaustedError("would wait for unrelated stacker reservation")

    monkeypatch.setattr(ops_slot, "acquire", would_wait)

    with pytest.raises(slot_mod.PoolExhaustedError, match="would wait"):
        service.sync(
            SelectorTarget(repo_name, "feature"),
            ScopeSpec(only=True),
            options=SyncOptions(offline=True),
        )

    assert git.rev_parse(reserved_slot.path, "HEAD") == original_head
    assert service.db.get_operation(repo_name) is None
    assert pooldb.get_owner(repo_name, reserved_slot.uuid) == OWNER_STACKER_OPS


def test_sync_rejects_exact_checkout_outside_pm_pool(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_name, repo_path = stacker_repo
    seed_slot = three_slots[0]
    notices: list[str] = []
    service = _service(pm_env, notices)
    pooldb = PoolDB(pm_env.pool_db())

    _initialize(service, repo_name, seed_slot, "feature")
    original_head = commit_file(seed_slot.path, "feature.txt", "feature\n", "feature work")
    git.checkout(seed_slot.path, "-b", "parking", "main")
    _advance_main(repo_path)
    git.checkout(repo_path, "feature")
    for index, slot in enumerate(three_slots):
        _claim_project(pooldb, slot, f"project-{index}")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(git.GitError, match=r"outside the PM pool|PM-managed"):
        service.sync(
            SelectorTarget(repo_name, "feature"),
            ScopeSpec(only=True),
            options=SyncOptions(offline=True),
        )

    assert git.rev_parse(repo_path, "HEAD") == original_head
    assert service.db.get_operation(repo_name) is None


def test_sync_preserves_pool_wait_when_checkout_is_genuinely_required(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_name, repo_path = stacker_repo
    seed_slot, project_slot, held_slot = three_slots
    notices: list[str] = []
    service = _service(pm_env, notices)
    pooldb = PoolDB(pm_env.pool_db())

    _initialize(service, repo_name, seed_slot, "feature")
    commit_file(seed_slot.path, "feature.txt", "feature\n", "feature work")
    git.checkout(seed_slot.path, "-b", "parking", "main")
    _claim_project(pooldb, seed_slot, "project-a")
    _claim_project(pooldb, project_slot, "project-b")
    pooldb.claim(held_slot.repo, held_slot.uuid, OWNER_STACKER_OPS)
    monkeypatch.chdir(tmp_path)
    _advance_main(repo_path)

    def release_later() -> None:
        time.sleep(0.1)
        pooldb.release(held_slot.repo, held_slot.uuid)

    threading.Thread(target=release_later, daemon=True).start()

    result = service.sync(
        SelectorTarget(repo_name, "feature"),
        ScopeSpec(only=True),
        options=SyncOptions(offline=True),
    )

    assert "Sync complete." in result
    assert any("Waiting for a stacker slot" in notice for notice in notices)
    assert pooldb.get_owner(repo_name, held_slot.uuid) is None
    assert service.db.get_operation(repo_name) is None


def test_sync_abort_preserves_borrowed_checkout_owner_and_pool_state(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, repo_path = stacker_repo
    parent_slot, current_slot, free_slot = three_slots
    notices: list[str] = []
    service = _service(pm_env, notices)
    pooldb = PoolDB(pm_env.pool_db())

    _initialize(service, repo_name, parent_slot, "parent")
    commit_file(parent_slot.path, "shared.txt", "parent\n", "parent conflict")
    _initialize(service, repo_name, current_slot, "leaf", parent="parent")
    commit_file(current_slot.path, "leaf.txt", "leaf\n", "leaf work")
    git.checkout(parent_slot.path, "-b", "parking", "main")
    _claim_project(pooldb, parent_slot, "other-project")
    _claim_project(pooldb, current_slot, "current-project")
    monkeypatch.chdir(current_slot.path)
    _advance_main(repo_path, conflict=True)

    paused = service.sync(
        SelectorTarget(repo_name, "leaf"),
        options=SyncOptions(offline=True),
    )

    assert "paused" in paused.lower()
    assert git.current_branch(current_slot.path) == "parent"
    assert pooldb.get_owner(repo_name, current_slot.uuid) == Owner(
        OwnerKind.PROJECT, "current-project"
    )
    assert pooldb.get_owner(repo_name, free_slot.uuid) is None

    aborted = service.abort_operation(repo_name)

    assert "Aborted" in aborted
    assert service.db.get_operation(repo_name) is None
    assert pooldb.get_owner(repo_name, current_slot.uuid) == Owner(
        OwnerKind.PROJECT, "current-project"
    )


def test_operation_lease_creation_does_not_overwrite_concurrent_sync(pm_env: Paths) -> None:
    db = StackerDB(pm_env.stacker_db())
    first = OperationState(
        repo_name="demo",
        op_type="local_sync",
        status="running",
        branch="first",
    )
    second = OperationState(
        repo_name="demo",
        op_type="local_sync",
        status="running",
        branch="second",
    )

    assert db.try_put_operation(first) is True
    assert db.try_put_operation(second) is False
    assert db.get_operation("demo") == first
