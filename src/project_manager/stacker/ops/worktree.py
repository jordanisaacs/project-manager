from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OWNER_STACKER_OPS, PoolDB
from project_manager.stacker import git, locate, ops_slot, selectors
from project_manager.stacker.models import OperationState, TrackedBranch
from project_manager.stacker.render import format as fmt

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


@dataclass
class _Acquired:
    """A worktree path to run git commands in, plus the ops slot if we acquired one.

    If `ops` is set, the caller claimed it via ops_slot.acquire. Clean completion
    must call release(). A paused/failed op must NOT release — the claim is the
    resumable state that `continue` / `abort` drive.
    """

    path: Path
    ops: slot_mod.Slot | None


def acquire(ctx: StackerCtx, repo_name: str, branch: str) -> _Acquired:
    existing = locate.locate_worktree(ctx.paths, repo_name, branch)
    if existing is not None:
        return _Acquired(path=existing, ops=None)
    claimed = ops_slot.acquire(
        ctx.paths,
        PoolDB(ctx.paths.pool_db()),
        repo_name,
        branch,
        wait=ops_slot.WaitOptions(progress=ctx.progress),
    )
    return _Acquired(path=claimed.path, ops=claimed)


def release_if_owned(ctx: StackerCtx, acquired: _Acquired) -> None:
    if acquired.ops is not None:
        ops_slot.release(PoolDB(ctx.paths.pool_db()), acquired.ops)


def slot_path_for_active_op(
    ctx: StackerCtx, op: OperationState, *, required: bool = True
) -> Path | None:
    if op.branch is None:
        return None
    path = locate.locate_worktree(ctx.paths, op.repo_name, op.branch)
    if path is None:
        if required:
            raise git.GitError(
                f"Operation references {selectors.selector_for(op.repo_name, op.branch)} "
                "but the branch is not checked out anywhere."
            )
        return None
    return path


def existing_ops_slot(
    ctx: StackerCtx, op: OperationState, slot_path: Path | None
) -> slot_mod.Slot | None:
    """Return the stacker-ops Slot handle backing this op's current branch, or None.

    A slot is an ops slot if the pool db records it as stacker-owned. A
    project-owned slot (or otherwise-claimed slot) returns None — we don't own it.
    """
    if slot_path is None:
        return None
    expected_pool = ctx.paths.pool(op.repo_name).resolve()
    if slot_path.parent.resolve() != expected_pool:
        return None
    pooldb = PoolDB(ctx.paths.pool_db())
    if pooldb.get_owner(op.repo_name, slot_path.name) != OWNER_STACKER_OPS:
        return None
    return slot_mod.Slot(repo=op.repo_name, uuid=slot_path.name, path=slot_path)


def require_checked_out(ctx: StackerCtx, repo_name: str, branch: str) -> Path:
    path = locate.locate_worktree(ctx.paths, repo_name, branch)
    if path is None:
        raise git.GitError(
            f"{selectors.selector_for(repo_name, branch)} is not checked out in any "
            "worktree. Check it out in a pm pool slot first."
        )
    return path


def repo_name_for_path(ctx: StackerCtx, worktree_path: Path) -> str | None:
    resolved = worktree_path.resolve()
    try:
        relative = resolved.relative_to(ctx.paths.worktrees.resolve())
    except ValueError:
        return None
    parts = relative.parts
    if not parts:
        return None
    return parts[0]


def run_single_pp(
    ctx: StackerCtx,
    tracked: TrackedBranch,
    logs: list[str],
    *,
    require_upstream_before: bool = False,
) -> bool:
    label = selectors.selector_for(tracked.repo_name, tracked.branch)
    path = require_checked_out(ctx, tracked.repo_name, tracked.branch)
    upstream = git.upstream_branch(path)
    if require_upstream_before and not upstream:
        fmt.record(ctx, logs, f"Stopping at {label}: no upstream remote is configured.")
        return False
    fmt.record(ctx, logs, f"Running git pp --force in {label}")
    # pp_force streams its output directly to the parent's stdout/stderr,
    # so the user sees push progress live instead of waiting silently.
    proc = git.pp_force(path)
    if proc.returncode != 0:
        raise git.GitError(f"git pp --force failed in {label}.")
    upstream_after = git.upstream_branch(path)
    if not upstream_after:
        fmt.record(ctx, logs, f"Stopping at {label}: no upstream remote is configured.")
        raise git.GitError(f"{label} has no upstream remote after git pp --force.")
    return True
