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
    fresh ops slot. Pool exhaustion in the last step raises
    `slot_mod.PoolExhaustedError` (same as today's `ops_slot.acquire`).
    """
    existing = locate.locate_worktree(ctx.paths, repo_name, branch)
    if existing is not None:
        return AcquiredSlot(path=existing, ops=None)
    cwd_path = _cwd_reuse_path(ctx, repo_name, cwd_reuse)
    if cwd_path is not None:
        git.git(cwd_path, "checkout", branch)
        return AcquiredSlot(path=cwd_path, ops=None)
    pooldb = PoolDB(ctx.paths.pool_db())
    claimed = ops_slot.acquire(
        ctx.paths, pooldb, repo_name, branch,
        wait=ops_slot.WaitOptions(progress=ctx.progress),
    )
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
    """
    if acquired.ops is not None:
        ops_slot.release(PoolDB(ctx.paths.pool_db()), acquired.ops)


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
