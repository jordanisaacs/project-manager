from __future__ import annotations

from pathlib import Path

import pytest

# Pre-load the CLI package before stacker.commands so the cyclopts root
# registration at commands/__init__.py:6 sees a fully-initialized
# project_manager.cli module.
import project_manager.cli  # noqa: F401
from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OWNER_STACKER_OPS, Owner, OwnerKind, PoolDB
from project_manager.stacker import git as stacker_git
from project_manager.stacker import slot
from project_manager.stacker.commands import _common
from project_manager.stacker.service import StackerService
from project_manager.stacker.slot import CwdReusePolicy


@pytest.fixture
def pooldb(pm_env: Paths) -> PoolDB:
    return PoolDB(pm_env.pool_db())


def test_reserve_for_new_branch_uses_current_slot_when_cwd_is_pm_slot(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    pooldb: PoolDB,
    service: StackerService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, _ = stacker_repo
    here = three_slots[0]
    monkeypatch.chdir(here.path)

    acquired = slot.reserve_for_new_branch(service.ctx, repo_name)
    assert acquired.path.resolve() == here.path.resolve()
    # Cwd reuse never claims, so ops is None and release is a no-op.
    assert acquired.ops is None
    slot.release_if_owned(service.ctx, acquired)
    assert pooldb.get_owner(here.repo, here.uuid) is None


def test_reserve_for_new_branch_claims_fresh_slot_when_cwd_outside_pool(  # noqa: PLR0913 (fixture plumbing)
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    pooldb: PoolDB,
    service: StackerService,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_name, _ = stacker_repo
    monkeypatch.chdir(tmp_path)

    acquired = slot.reserve_for_new_branch(service.ctx, repo_name)
    slot_uuids = {s.uuid for s in three_slots}
    assert acquired.path.parent.resolve() == service.paths.pool(repo_name).resolve()
    assert acquired.path.name in slot_uuids
    assert acquired.ops is not None
    assert pooldb.get_owner(repo_name, acquired.path.name) == OWNER_STACKER_OPS
    slot.release_if_owned(service.ctx, acquired)
    assert pooldb.get_owner(repo_name, acquired.path.name) is None


def test_reserve_for_new_branch_claims_fresh_slot_when_cwd_is_in_different_repo(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Cwd is inside `demo`'s pool but the create call asks for a different
    # repo. Should fall through to ops_slot.claim against `other`, which
    # has no slots, so we get PoolExhaustedError.
    repo_name, _ = stacker_repo
    monkeypatch.chdir(three_slots[0].path)

    other_pool = pm_env.pool("other")
    other_pool.mkdir()
    with pytest.raises(slot_mod.PoolExhaustedError):
        slot.reserve_for_new_branch(service.ctx, "other")
    assert repo_name != "other"


def test_resolve_slot_returns_existing_checkout(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    pooldb: PoolDB,
    service: StackerService,
) -> None:
    """Locate-fast-path: branch already checked out → return that worktree."""
    repo_name, repo_path = stacker_repo
    stacker_git.git(repo_path, "branch", "feature-x", "main")
    holder = three_slots[0]
    stacker_git.git(holder.path, "checkout", "feature-x")

    acquired = slot.resolve_slot(service.ctx, repo_name, "feature-x")
    assert acquired.path.resolve() == holder.path.resolve()
    assert acquired.ops is None
    # Slot ownership unchanged — no new claim.
    assert pooldb.get_owner(holder.repo, holder.uuid) is None


def test_resolve_slot_reuses_detached_cwd_slot_for_sync(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    pooldb: PoolDB,
    service: StackerService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync-style policy (default): cwd detached → reuse cwd, no fresh claim."""
    repo_name, repo_path = stacker_repo
    stacker_git.git(repo_path, "branch", "feature-y", "main")
    here = three_slots[0]  # three_slots returns detached HEAD slots
    monkeypatch.chdir(here.path)

    acquired = slot.resolve_slot(service.ctx, repo_name, "feature-y")
    assert acquired.path.resolve() == here.path.resolve()
    assert acquired.ops is None
    assert stacker_git.current_branch(here.path) == "feature-y"
    # No claim taken, so the slot stays free.
    assert pooldb.get_owner(here.repo, here.uuid) is None


def test_resolve_slot_skips_cwd_when_on_other_live_branch(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    pooldb: PoolDB,
    service: StackerService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default policy refuses to clobber a live cwd branch — falls through."""
    repo_name, repo_path = stacker_repo
    stacker_git.git(repo_path, "branch", "feature-target", "main")
    cwd_slot = three_slots[0]
    # Project-claim cwd_slot so ops_slot.claim cannot grab it.
    pooldb.claim(
        cwd_slot.repo, cwd_slot.uuid, Owner(OwnerKind.PROJECT, "user-work"),
    )
    stacker_git.git(cwd_slot.path, "checkout", "-b", "user-feature")
    monkeypatch.chdir(cwd_slot.path)

    acquired = slot.resolve_slot(service.ctx, repo_name, "feature-target")
    # Cwd was on `user-feature`, so resolve_slot must NOT pick it; instead
    # an ops slot is freshly claimed elsewhere.
    assert acquired.path.resolve() != cwd_slot.path.resolve()
    assert acquired.ops is not None
    assert stacker_git.current_branch(cwd_slot.path) == "user-feature"
    slot.release_if_owned(service.ctx, acquired)


def test_resolve_slot_clobbers_cwd_branch_when_allow_branch_switch(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    pooldb: PoolDB,
    service: StackerService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adoption-style policy: cwd on a live branch → still reuse, switch branch."""
    repo_name, repo_path = stacker_repo
    stacker_git.git(repo_path, "branch", "feature-target", "main")
    here = three_slots[0]
    stacker_git.git(here.path, "checkout", "-b", "user-feature")
    monkeypatch.chdir(here.path)

    acquired = slot.resolve_slot(
        service.ctx,
        repo_name,
        "feature-target",
        cwd_reuse=CwdReusePolicy(allow_branch_switch=True),
    )
    assert acquired.path.resolve() == here.path.resolve()
    assert acquired.ops is None
    assert stacker_git.current_branch(here.path) == "feature-target"
    assert pooldb.get_owner(here.repo, here.uuid) is None


def test_resolve_slot_disabled_policy_skips_cwd(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`enabled=False` skips cwd reuse even when cwd is detached and free."""
    repo_name, repo_path = stacker_repo
    stacker_git.git(repo_path, "branch", "feature-z", "main")
    here = three_slots[0]
    monkeypatch.chdir(here.path)

    acquired = slot.resolve_slot(
        service.ctx, repo_name, "feature-z",
        cwd_reuse=CwdReusePolicy(enabled=False),
    )
    # Cwd was eligible (detached, same repo) but policy disabled — fresh ops slot used.
    assert acquired.ops is not None
    slot.release_if_owned(service.ctx, acquired)


def test_resolve_repo_uses_cwd_slot_when_args_empty(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, _ = stacker_repo
    monkeypatch.chdir(three_slots[0].path)
    assert _common.resolve_repo(None, pm_env) == repo_name


@pytest.mark.usefixtures("stacker_repo")
def test_resolve_repo_prefers_explicit_arg(
    pm_env: Paths,
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(three_slots[0].path)
    assert _common.resolve_repo("explicit", pm_env) == "explicit"


def test_resolve_repo_errors_when_outside_slot_and_no_arg(
    pm_env: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(stacker_git.GitError, match="pass --repo"):
        _common.resolve_repo(None, pm_env)


@pytest.mark.usefixtures("stacker_repo")
def test_resolve_branch_uses_cwd_current_branch(
    pm_env: Paths,
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    s = three_slots[0]
    stacker_git.git(s.path, "checkout", "-b", "feature-cwd")
    monkeypatch.chdir(s.path)
    assert _common.resolve_branch(None, pm_env) == "feature-cwd"


@pytest.mark.usefixtures("stacker_repo")
def test_resolve_branch_errors_on_detached_head(
    pm_env: Paths,
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(three_slots[0].path)  # three_slots creates them detached
    with pytest.raises(stacker_git.GitError, match="detached"):
        _common.resolve_branch(None, pm_env)
