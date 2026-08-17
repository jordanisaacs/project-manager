import contextlib
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import Owner, OwnerKind, PoolDB
from project_manager.pool.slot import PoolExhaustedError, Slot, SlotBusyError
from project_manager.project import branch as branch_mod
from project_manager.project import db, lease
from project_manager.project.branch import RestoreResult
from project_manager.render import Column


@dataclass(frozen=True)
class AttachedWt:
    wt: str
    repo: str
    uuid: str
    path: Path


ATTACHED_COLUMNS: list[Column] = [
    Column("Worktree", "wt"),
    Column("Repo", "repo", style="blue"),
    Column("UUID", "uuid", style="dim"),
    Column("Path", "path"),
]


@dataclass(frozen=True)
class AttachResult:
    newly_claimed: list[AttachedWt] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _AttachCtx:
    paths: Paths
    pooldb: PoolDB
    conn: sqlite3.Connection


def _resolve_wts(
    project: str,
    wts: list[str] | None,
    all_rows: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    if wts is None:
        return all_rows
    known = {name: (repo, uuid) for name, repo, uuid in all_rows}
    missing = [w for w in wts if w not in known]
    if missing:
        raise ProjectError(f"project '{project}' has no such worktree(s): {', '.join(missing)}")
    return [(w, *known[w]) for w in wts]


def _try_reclaim(
    paths: Paths,
    pooldb: PoolDB,
    owner: Owner,
    repo: str,
    slot_uuid: str,
) -> Slot | None:
    """Attempt to reclaim the remembered slot. Returns the claimed Slot, or None if unavailable."""
    slot_path = paths.slot(repo, slot_uuid)
    if not slot_path.is_dir():
        return None
    if not pooldb.is_free(repo, slot_uuid):
        return None
    try:
        pooldb.claim(repo, slot_uuid, owner)
    except SlotBusyError:
        return None
    return Slot(repo=repo, uuid=slot_uuid, path=slot_path)


def _claim_fallback(
    paths: Paths,
    pooldb: PoolDB,
    owner: Owner,
    repo: str,
    retries: int = 3,
) -> Slot:
    for _ in range(retries):
        free = slot_mod.free_slots(paths, pooldb, repo)
        if not free:
            raise PoolExhaustedError(
                f"no free slots for repo '{repo}' — run `pm pool add {repo}` or "
                f"detach/delete a project"
            )
        for s in free:
            try:
                pooldb.claim(repo, s.uuid, owner)
            except SlotBusyError:
                continue
            else:
                return s
    raise SlotBusyError(f"lost {retries} claim races for repo '{repo}'")


def _attach_one(
    ctx: _AttachCtx,
    project: str,
    wt: str,
    repo: str,
    remembered_uuid: str,
) -> Slot | None:
    """Attach a single worktree. Returns the newly-claimed slot, or None if already attached."""
    forward = ctx.paths.forward(project, wt)
    owner = Owner(OwnerKind.PROJECT, project)

    if forward.is_symlink():
        target = forward.readlink()
        existing = ctx.pooldb.get_owner(repo, target.name)
        if existing == owner:
            return None  # already active (idempotent)
        raise ProjectError(f"{forward} exists but is not owned by this project — run `pm check`")

    s = _try_reclaim(ctx.paths, ctx.pooldb, owner, repo, remembered_uuid)
    if s is None:
        s = _claim_fallback(ctx.paths, ctx.pooldb, owner, repo)
        db.update_slot(ctx.conn, wt, s.uuid)
    try:
        forward.symlink_to(s.path)
    except BaseException:
        ctx.pooldb.release(repo, s.uuid)
        raise
    return s


def _maybe_restore_branch(
    ctx: _AttachCtx,
    wt: str,
    repo: str,
    slot_path: Path,
    *,
    no_branch: bool,
) -> str | None:
    """Restore saved branch into slot and clear the saved field.

    Always clears `branch` on the row after attaching so in-session branch
    changes by the user aren't accidentally reverted by a future attach.
    Returns a human-readable warning string if restoration was skipped, else
    None.
    """
    saved = db.get_branch(ctx.conn, wt)
    if saved is None:
        # A free slot may predate submodule-aware release or may have been
        # advanced by another lifecycle path. Repair it even when there is
        # no saved branch to restore, so attach never surfaces gitlink-only
        # dirtiness to the new project owner.
        branch_mod.synchronize_submodules(slot_path)
        return None
    warning: str | None = None
    if not no_branch:
        outcome = branch_mod.restore(slot_path, ctx.paths.repo(repo), saved)
        if outcome.result == RestoreResult.SKIPPED_IN_USE:
            warning = (
                f"{wt}: saved branch '{saved}' already checked out at "
                f"{outcome.conflict_path}; slot left on default"
            )
        elif outcome.result == RestoreResult.SKIPPED_MISSING_BRANCH:
            warning = f"{wt}: saved branch '{saved}' no longer exists; slot left on default"
        if outcome.result != RestoreResult.RESTORED:
            branch_mod.synchronize_submodules(slot_path)
    else:
        branch_mod.synchronize_submodules(slot_path)
    db.set_branch(ctx.conn, wt, None)
    return warning


def attach(
    paths: Paths,
    project: str,
    wts: list[str] | None,
    *,
    no_branch: bool = False,
) -> AttachResult:
    """Attach the given worktrees (or all if `wts is None`).

    For each row in the db: try the remembered `slot_uuid`. If the slot is missing
    or not free, claim any free slot and UPDATE the row. Best-effort rollback
    across worktrees on failure.

    After a fresh claim, restore the saved branch (unless `no_branch=True`). The
    saved branch field is unconditionally cleared on successful attach.

    Returns AttachResult with newly-claimed worktrees and any per-wt warnings
    (e.g., saved branch already checked out elsewhere).
    """
    db_path = paths.project_db(project)
    if not db_path.is_file():
        raise ProjectError(f"project '{project}' does not exist")

    pooldb = PoolDB(paths.pool_db())
    attached: list[tuple[str, str, Slot]] = []  # (wt, repo, slot)
    warnings: list[str] = []
    try:
        with db.transaction(db_path, immediate=True) as conn:
            lease.require_mutable(conn, project)
            ctx = _AttachCtx(paths=paths, pooldb=pooldb, conn=conn)
            to_attach = _resolve_wts(project, wts, db.list_wts(conn))
            for wt, repo, remembered in to_attach:
                s = _attach_one(ctx, project, wt, repo, remembered)
                if s is not None:
                    warning = _maybe_restore_branch(
                        ctx,
                        wt,
                        repo,
                        s.path,
                        no_branch=no_branch,
                    )
                    if warning:
                        warnings.append(warning)
                    attached.append((wt, repo, s))
    except BaseException:
        for wt, repo, s in reversed(attached):
            forward = paths.forward(project, wt)
            with contextlib.suppress(FileNotFoundError):
                forward.unlink()
            pooldb.release(repo, s.uuid)
        raise
    return AttachResult(
        newly_claimed=[
            AttachedWt(wt=wt, repo=repo, uuid=s.uuid, path=s.path) for wt, repo, s in attached
        ],
        warnings=warnings,
    )
