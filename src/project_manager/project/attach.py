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
from project_manager.project import db
from project_manager.project.branch import RestoreResult


@dataclass(frozen=True)
class AttachResult:
    newly_claimed: list[Slot] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _resolve_repos(
    project: str,
    repos: list[str] | None,
    all_rows: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    if repos is None:
        return all_rows
    known = dict(all_rows)
    missing = [r for r in repos if r not in known]
    if missing:
        raise ProjectError(
            f"project '{project}' has no such repo(s): {', '.join(missing)}"
        )
    return [(r, known[r]) for r in repos]


def _try_reclaim(
    paths: Paths, pooldb: PoolDB, owner: Owner, repo: str, slot_uuid: str,
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
    paths: Paths, pooldb: PoolDB, owner: Owner, repo: str, retries: int = 3,
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


def _attach_one(  # noqa: PLR0913
    paths: Paths,
    pooldb: PoolDB,
    project: str,
    repo: str,
    remembered_uuid: str,
    conn: sqlite3.Connection,
) -> Slot | None:
    """Attach a single repo. Returns the newly-claimed slot, or None if already attached."""
    forward = paths.forward(project, repo)
    owner = Owner(OwnerKind.PROJECT, project)

    if forward.is_symlink():
        target = forward.readlink()
        existing = pooldb.get_owner(repo, target.name)
        if existing == owner:
            return None  # already active (idempotent)
        raise ProjectError(
            f"{forward} exists but is not owned by this project — run `pm check`"
        )

    s = _try_reclaim(paths, pooldb, owner, repo, remembered_uuid)
    if s is None:
        s = _claim_fallback(paths, pooldb, owner, repo)
        db.update_slot(conn, repo, s.uuid)
    try:
        forward.symlink_to(s.path)
    except BaseException:
        pooldb.release(repo, s.uuid)
        raise
    return s


def _maybe_restore_branch(
    paths: Paths,
    conn: sqlite3.Connection,
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
    saved = db.get_branch(conn, repo)
    if saved is None:
        return None
    warning: str | None = None
    if not no_branch:
        outcome = branch_mod.restore(slot_path, paths.repo(repo), saved)
        if outcome.result == RestoreResult.SKIPPED_IN_USE:
            warning = (
                f"{repo}: saved branch '{saved}' already checked out at "
                f"{outcome.conflict_path}; slot left on default"
            )
        elif outcome.result == RestoreResult.SKIPPED_MISSING_BRANCH:
            warning = (
                f"{repo}: saved branch '{saved}' no longer exists; "
                f"slot left on default"
            )
    db.set_branch(conn, repo, None)
    return warning


def attach(
    paths: Paths,
    project: str,
    repos: list[str] | None,
    *,
    no_branch: bool = False,
) -> AttachResult:
    """Attach the given repos (or all if `repos is None`).

    For each repo row in the db: try the remembered `slot_uuid`. If the slot is missing
    or not free, claim any free slot and UPDATE the row. Best-effort rollback across
    repos on failure.

    After a fresh claim, restore the saved branch (unless `no_branch=True`). The
    saved branch field is unconditionally cleared on successful attach.

    Returns AttachResult with newly-claimed slots and any per-repo warnings
    (e.g., saved branch already checked out elsewhere).
    """
    db_path = paths.project_db(project)
    if not db_path.is_file():
        raise ProjectError(f"project '{project}' does not exist")

    pooldb = PoolDB(paths.pool_db())
    attached: list[tuple[str, Slot]] = []
    warnings: list[str] = []
    try:
        with db.transaction(db_path) as conn:
            to_attach = _resolve_repos(project, repos, db.list_repos(conn))
            for repo, remembered in to_attach:
                s = _attach_one(paths, pooldb, project, repo, remembered, conn)
                if s is not None:
                    warning = _maybe_restore_branch(
                        paths, conn, repo, s.path, no_branch=no_branch,
                    )
                    if warning:
                        warnings.append(warning)
                    attached.append((repo, s))
    except BaseException:
        for repo, s in reversed(attached):
            forward = paths.forward(project, repo)
            with contextlib.suppress(FileNotFoundError):
                forward.unlink()
            pooldb.release(repo, s.uuid)
        raise
    return AttachResult(newly_claimed=[s for _, s in attached], warnings=warnings)
