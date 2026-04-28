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


def test_release_if_clean_no_op_when_not_owned(
    stacker_repo: tuple[str, Path],  # noqa: ARG001 — bootstraps demo repo for service
    pooldb: PoolDB,
    service: StackerService,
) -> None:
    """`acquired.ops is None` (locate-fast-path or cwd-reuse) is a no-op."""
    not_owned = slot.AcquiredSlot(path=Path("/dev/null"), ops=None)
    slot.release_if_clean(service.ctx, not_owned)  # must not raise
    assert pooldb.list_owned("demo") == []


def test_release_if_clean_releases_when_worktree_clean(
    stacker_repo: tuple[str, Path],  # noqa: ARG001 — bootstraps demo repo
    three_slots: list[slot_mod.Slot],
    pooldb: PoolDB,
    service: StackerService,
) -> None:
    pooldb.claim(three_slots[0].repo, three_slots[0].uuid, OWNER_STACKER_OPS)
    stacker_git.git(three_slots[0].path, "checkout", "-b", "feature-clean")
    acquired = slot.AcquiredSlot(path=three_slots[0].path, ops=three_slots[0])

    slot.release_if_clean(service.ctx, acquired)

    assert pooldb.get_owner(three_slots[0].repo, three_slots[0].uuid) is None
    # Released slots are detached so the next claimant doesn't inherit a branch.
    head = stacker_git.git(
        three_slots[0].path, "symbolic-ref", "-q", "HEAD", check=False,
    )
    assert head.returncode != 0


def test_release_if_clean_holds_when_worktree_has_tracked_changes(
    stacker_repo: tuple[str, Path],  # noqa: ARG001 — bootstraps demo repo
    three_slots: list[slot_mod.Slot],
    pooldb: PoolDB,
    service: StackerService,
) -> None:
    pooldb.claim(three_slots[0].repo, three_slots[0].uuid, OWNER_STACKER_OPS)
    stacker_git.git(three_slots[0].path, "checkout", "-b", "feature-dirty")
    # Modify a tracked file so `git status --porcelain -uno` is non-empty.
    (three_slots[0].path / "README.md").write_text("# changed\n")
    acquired = slot.AcquiredSlot(path=three_slots[0].path, ops=three_slots[0])

    slot.release_if_clean(service.ctx, acquired)

    # Slot stays claimed; the user's modification would otherwise be discarded
    # by the detach-HEAD step inside `ops_slot.release`.
    assert pooldb.get_owner(three_slots[0].repo, three_slots[0].uuid) == OWNER_STACKER_OPS
    assert stacker_git.current_branch(three_slots[0].path) == "feature-dirty"


def test_release_if_clean_holds_when_cherry_pick_in_progress(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    pooldb: PoolDB,
    service: StackerService,
) -> None:
    """`CHERRY_PICK_HEAD` indicates a paused cherry-pick — `pm stacker continue`
    must find the slot still claimed when it resumes."""
    _repo_name, _ = stacker_repo
    # Stage a deliberate cherry-pick conflict: two divergent commits on the
    # same line, then cherry-pick one onto the other so the cherry-pick
    # pauses with CHERRY_PICK_HEAD set. All work happens in slot[0] so it
    # doesn't fight the main repo's worktree.
    work = three_slots[0]
    stacker_git.git(work.path, "checkout", "-b", "feature-side", "main")
    (work.path / "README.md").write_text("side\n")
    stacker_git.git(work.path, "add", "README.md")
    stacker_git.git(
        work.path,
        "-c", "user.email=t@e.com", "-c", "user.name=t",
        "commit", "-m", "side: divergent",
    )
    side_sha = stacker_git.rev_parse(work.path, "HEAD")

    stacker_git.git(work.path, "checkout", "-b", "feature-main", "main")
    (work.path / "README.md").write_text("main\n")
    stacker_git.git(work.path, "add", "README.md")
    stacker_git.git(
        work.path,
        "-c", "user.email=t@e.com", "-c", "user.name=t",
        "commit", "-m", "main: divergent",
    )

    stacker_git.git(work.path, "cherry-pick", side_sha, check=False)
    # Conflict — `CHERRY_PICK_HEAD` exists.
    assert stacker_git.cherry_pick_in_progress(work.path)

    pooldb.claim(work.repo, work.uuid, OWNER_STACKER_OPS)
    acquired = slot.AcquiredSlot(path=work.path, ops=work)

    slot.release_if_clean(service.ctx, acquired)

    # CHERRY_PICK_HEAD survives → slot must too, so `continue` can drive it.
    assert pooldb.get_owner(work.repo, work.uuid) == OWNER_STACKER_OPS
    assert stacker_git.cherry_pick_in_progress(work.path)


def test_acquired_for_op_releases_on_kbinterrupt_when_clean(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],  # noqa: ARG001 — pool-population side-effect
    pooldb: PoolDB,
    service: StackerService,
) -> None:
    """The context manager must release on `KeyboardInterrupt` — that's the
    cancel-during-sync case the broader cleanup is targeted at."""
    repo_name, repo_path = stacker_repo
    stacker_git.git(repo_path, "branch", "feature-target", "main")

    with pytest.raises(KeyboardInterrupt), slot.acquired_for_op(
        service.ctx, repo_name, "feature-target",
    ):
        raise KeyboardInterrupt

    stacker_owned = [
        uuid for repo, uuid, owner in pooldb.list_owned(repo_name)
        if owner.kind == OwnerKind.STACKER
    ]
    assert stacker_owned == [], (
        "interrupt with a clean worktree must not leak the stacker claim"
    )


def test_resolve_slot_falls_back_to_cwd_when_pool_exhausted(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    pooldb: PoolDB,
    service: StackerService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Last-resort fallback: pool fully claimed by projects and cwd is on a
    live branch — default policy says "skip cwd" and `ops_slot.claim`
    fails fast (no stacker-owned slot to wait on). Without a fallback the
    user sees `pool ... has no free slot...` and is stuck. Reuse cwd
    instead, switching the live branch — same as `allow_branch_switch=True`
    but only when the pool can't help.
    """
    repo_name, repo_path = stacker_repo
    stacker_git.git(repo_path, "branch", "feature-target", "main")
    # All slots project-owned: no free slot, no stacker slot to wait on.
    for i, s in enumerate(three_slots):
        pooldb.claim(s.repo, s.uuid, Owner(OwnerKind.PROJECT, f"proj-{i}"))
    cwd_slot = three_slots[0]
    stacker_git.git(cwd_slot.path, "checkout", "-b", "user-feature")
    monkeypatch.chdir(cwd_slot.path)

    acquired = slot.resolve_slot(service.ctx, repo_name, "feature-target")

    assert acquired.path.resolve() == cwd_slot.path.resolve(), (
        "pool exhausted → resolve_slot should fall back to cwd reuse"
    )
    assert stacker_git.current_branch(cwd_slot.path) == "feature-target"
    assert acquired.ops is None, "cwd-reuse fallback must not take a stacker claim"


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
