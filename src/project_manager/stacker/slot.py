"""Unified worktree-acquisition primitive for stacker commands.

Every stacker op that needs a checkout to run git in funnels through here.
The resolution order is the same in all three flavors:

1. The branch is already checked out somewhere → use that worktree.
2. The cwd is a pm pool slot for the same repo and is "available" by
   the supplied policy → check out the branch there.
3. Mint a fresh ops slot via `ops_slot`.

Step (2) is what made it possible to do `pm stacker create --replace` and
`pm stacker sync` on the cwd slot after the user explicitly detached HEAD
to free it; previously each flow had its own ad-hoc resolution.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from project_manager.pool import slot as slot_mod
from project_manager.pool.db import PoolDB

from . import git, locate, ops_slot

if TYPE_CHECKING:
    from .ctx import StackerCtx


@dataclass(frozen=True)
class CwdReusePolicy:
    """Decides whether `resolve_slot` is allowed to reuse the cwd's pm slot.

    `enabled=False` skips the cwd step entirely (used by tests / callers that
    want the pre-librarification behavior).

    `allow_branch_switch=True` lets the cwd be reused even when it has a live
    branch — the caller has explicitly opted in to clobbering the cwd's
    current branch, e.g. `pm stacker create` or `pm stacker create --replace`.
    With the default `False`, cwd reuse only happens when cwd is detached
    (no branch to overwrite). Sync/push/etc. keep the default so an
    in-progress workflow in the cwd is preserved.
    """

    enabled: bool = True
    allow_branch_switch: bool = False


@dataclass
class AcquiredSlot:
    """A worktree path to run git in, plus the ops claim if `resolve_slot` minted one.

    `ops is not None` iff a fresh ops slot was claimed; that's the only case
    `release_if_owned` actually releases. Locate-fast-path and cwd-reuse hits
    return `ops=None` because no claim was taken.
    """

    path: Path
    ops: slot_mod.Slot | None


def resolve_slot(
    ctx: StackerCtx,
    repo_name: str,
    branch: str,
    *,
    cwd_reuse: CwdReusePolicy = CwdReusePolicy(),
) -> AcquiredSlot:
    """Pick a worktree to run an op for `branch` in, checking it out as needed.

    Resolution order: existing checkout → cwd reuse (when policy permits) →
    fresh ops slot → cwd reuse with branch-switch (last-resort fallback).
    The fallback kicks in only when `ops_slot.acquire` raises
    `PoolExhaustedError`; without it the user is stuck whenever every slot
    is project-claimed and they're sitting in one of those slots — adding
    a slot just to push is heavyweight, and clobbering the cwd's branch
    is recoverable (cwd is the user's, by definition).
    """
    existing = locate.locate_worktree(ctx.paths, repo_name, branch)
    if existing is not None:
        return AcquiredSlot(path=existing, ops=None)
    cwd_path = _cwd_reuse_path(ctx, repo_name, cwd_reuse)
    if cwd_path is not None:
        git.git(cwd_path, "checkout", branch)
        return AcquiredSlot(path=cwd_path, ops=None)
    pooldb = PoolDB(ctx.paths.pool_db())
    try:
        claimed = ops_slot.acquire(
            ctx.paths, pooldb, repo_name, branch,
            wait=ops_slot.WaitOptions(progress=ctx.progress),
        )
    except slot_mod.PoolExhaustedError:
        fallback = _cwd_reuse_path(
            ctx, repo_name,
            CwdReusePolicy(enabled=cwd_reuse.enabled, allow_branch_switch=True),
        )
        if fallback is None:
            raise
        git.git(fallback, "checkout", branch)
        return AcquiredSlot(path=fallback, ops=None)
    return AcquiredSlot(path=claimed.path, ops=claimed)


def reserve_for_new_branch(
    ctx: StackerCtx,
    repo_name: str,
    *,
    cwd_reuse: CwdReusePolicy = CwdReusePolicy(allow_branch_switch=True),
) -> AcquiredSlot:
    """Pick a worktree for fresh-branch creation; caller does the actual `git branch`.

    Skips the locate-by-branch step (the branch doesn't exist yet) and goes
    straight to cwd preference, then ops_slot.claim. The default policy
    matches the historical `_resolve_create_slot` behavior of clobbering
    the cwd's current branch when reusing.
    """
    cwd_path = _cwd_reuse_path(ctx, repo_name, cwd_reuse)
    if cwd_path is not None:
        return AcquiredSlot(path=cwd_path, ops=None)
    pooldb = PoolDB(ctx.paths.pool_db())
    claimed = ops_slot.claim(
        ctx.paths, pooldb, repo_name,
        wait=ops_slot.WaitOptions(progress=ctx.progress),
    )
    return AcquiredSlot(path=claimed.path, ops=claimed)


def release_if_owned(ctx: StackerCtx, acquired: AcquiredSlot) -> None:
    """Release a fresh ops slot. No-op when the slot was a cwd reuse or
    locate-fast-path hit (i.e. `acquired.ops is None`).

    Prefer `release_if_clean` for steady-state post-op cleanup — this
    variant is for paths that have already decided the worktree state
    doesn't need preserving (e.g. early-return after a no-op sync).
    """
    if acquired.ops is not None:
        ops_slot.release(PoolDB(ctx.paths.pool_db()), acquired.ops)


@contextlib.contextmanager
def acquired_for_op(
    ctx: StackerCtx,
    repo_name: str,
    branch: str,
    *,
    cwd_reuse: CwdReusePolicy = CwdReusePolicy(),
) -> Iterator[AcquiredSlot]:
    """Context-managed slot acquisition with automatic clean-release.

    Wraps `resolve_slot` + `release_if_clean` into a single `with`
    block — the body of the block is the op, and on exit (success,
    exception, `KeyboardInterrupt`) the slot is returned to the pool
    iff the worktree has nothing to preserve. A mid-cherry-pick
    (`CHERRY_PICK_HEAD` present) or uncommitted change keeps the slot
    held so `pm stacker continue` can resume.

    Use for any single-acquire op (push iteration, sync setup, absorb,
    create, etc.). The cherry-pick driver manages slot handoffs across
    its own queue and uses `release_if_clean` directly.
    """
    acquired = resolve_slot(ctx, repo_name, branch, cwd_reuse=cwd_reuse)
    try:
        yield acquired
    finally:
        release_if_clean(ctx, acquired)


def release_if_clean(ctx: StackerCtx, acquired: AcquiredSlot) -> None:
    """Release a stacker-minted slot iff the worktree has nothing to
    preserve.

    Designed for `try/finally` cleanup that fires on success, exception,
    and `KeyboardInterrupt` alike. Returns the slot to the pool when:
      - The slot wasn't stacker-minted (`acquired.ops is None`).
      - The worktree has no resumable state (clean tree, no
        `CHERRY_PICK_HEAD`).
    Otherwise holds the slot so `pm stacker continue` can pick up the
    user's mid-flight resolution. Resume relies on the DB-side
    `OperationState`, not on which slot the work lives in — releasing a
    clean slot is safe because the next continue rebinds via that state.
    """
    if acquired.ops is None:
        return
    try_release_ops_slot(ctx, acquired.ops.repo, acquired.ops.uuid)


def try_release_ops_slot(ctx: StackerCtx, repo: str, uuid: str) -> bool:
    """Release the stacker-ops slot at (repo, uuid) when its worktree has
    no resumable state. Returns True if the slot was released, False if
    held.

    Single source of truth for "is this stacker slot reclaimable?" —
    consulted by both `release_if_clean` (post-op cleanup) and
    `pm check --fix` (steady-state recovery of leaked claims). The
    predicate (`git.has_resumable_state`) and the action
    (`ops_slot.release` → `git.detach_head` + `pooldb.release`) live
    here so future callers don't drift into divergent dirty checks.

    Caller is responsible for already knowing the slot is owned by
    `OWNER_STACKER_OPS`; the function will silently no-op if not, since
    it only constructs the `slot_mod.Slot` view used by
    `ops_slot.release` and that release is itself idempotent against
    a missing pool row.
    """
    slot_path = ctx.paths.slot(repo, uuid)
    if git.has_resumable_state(slot_path):
        return False
    ops_slot.release(
        PoolDB(ctx.paths.pool_db()),
        slot_mod.Slot(repo=repo, uuid=uuid, path=slot_path),
    )
    return True


def _cwd_reuse_path(
    ctx: StackerCtx, repo_name: str, policy: CwdReusePolicy
) -> Path | None:
    if not policy.enabled:
        return None
    here = locate.slot_for_cwd(ctx.paths)
    if here is None or here.repo_name != repo_name:
        return None
    on_branch = git.current_branch(here.path)
    if on_branch and not policy.allow_branch_switch:
        return None
    return here.path
