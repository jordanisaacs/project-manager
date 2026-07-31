"""Durable, generic leases on a project's attached-worktree topology."""

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.pool.db import Owner, OwnerKind, PoolDB
from project_manager.project import db, discovery

_MAX_KEY_LENGTH = 255
PENDING_TTL_SECONDS = 10 * 60
_PENDING = "pending"
_FINALIZED = "finalized"


@dataclass(frozen=True)
class _AcquireMode:
    state: str
    expires_at: int | None
    now: int | None = None


@dataclass(frozen=True)
class LeaseWorktree:
    """One healthy attached worktree captured by a project lease."""

    name: str
    repo: str
    slot_uuid: str
    path: Path

    def __pm_json__(self) -> dict[str, object]:
        return {
            "name": self.name,
            "repo": self.repo,
            "slot_uuid": self.slot_uuid,
            "path": str(self.path),
        }


@dataclass(frozen=True)
class ProjectLease:
    """A durable holder/id lease and its immutable worktree snapshot."""

    project: str
    path: Path
    holder: str
    lease_id: str
    acquired_at: int
    state: str
    expires_at: int | None
    worktrees: tuple[LeaseWorktree, ...]

    def __pm_json__(self) -> dict[str, object]:
        return {
            "project": self.project,
            "path": str(self.path),
            "holder": self.holder,
            "lease_id": self.lease_id,
            "acquired_at": self.acquired_at,
            "state": self.state,
            "expires_at": self.expires_at,
            "worktrees": [worktree.__pm_json__() for worktree in self.worktrees],
        }


@dataclass(frozen=True)
class LeaseRelease:
    """Result of an idempotent lease release."""

    project: str
    holder: str
    lease_id: str
    released: bool


def _key(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} must not be empty")
    if len(normalized) > _MAX_KEY_LENGTH:
        raise ValueError(f"{label} must be at most {_MAX_KEY_LENGTH} characters")
    if "\n" in normalized or "\r" in normalized:
        raise ValueError(f"{label} must be a single line")
    return normalized


def _stored_worktrees(
    paths: Paths,
    project: str,
    conn: sqlite3.Connection,
    holder: str,
    lease_id: str,
) -> tuple[LeaseWorktree, ...]:
    return tuple(
        LeaseWorktree(
            name=name,
            repo=repo,
            slot_uuid=slot_uuid,
            path=paths.forward(project, name),
        )
        for name, repo, slot_uuid in conn.execute(
            "SELECT name, repo, slot_uuid "
            "FROM project_lease_worktrees "
            "WHERE holder = ? AND lease_id = ? ORDER BY name",
            (holder, lease_id),
        ).fetchall()
    )


def _healthy_worktrees(
    paths: Paths,
    project: str,
    conn: sqlite3.Connection,
) -> tuple[LeaseWorktree, ...]:
    pooldb = PoolDB(paths.pool_db())
    expected_owner = Owner(OwnerKind.PROJECT, project)
    worktrees: list[LeaseWorktree] = []
    for name, repo, remembered_uuid in db.list_wts(conn):
        forward = paths.forward(project, name)
        if not forward.is_symlink():
            continue
        target = forward.readlink()
        slot_uuid = target.name
        if (
            slot_uuid != remembered_uuid
            or not target.is_dir()
            or pooldb.get_owner(repo, slot_uuid) != expected_owner
        ):
            continue
        worktrees.append(
            LeaseWorktree(
                name=name,
                repo=repo,
                slot_uuid=slot_uuid,
                path=forward,
            )
        )
    return tuple(worktrees)


def _active_pairs(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (holder, lease_id)
        for holder, lease_id in conn.execute(
            "SELECT holder, lease_id FROM project_leases ORDER BY holder, lease_id"
        ).fetchall()
    ]


def cleanup_expired(conn: sqlite3.Connection, *, now: int | None = None) -> int:
    """Remove expired pending leases inside the caller's transaction."""
    cutoff = int(time.time()) if now is None else now
    cursor = conn.execute(
        "DELETE FROM project_leases "
        "WHERE state = 'pending' AND expires_at IS NOT NULL AND expires_at <= ?",
        (cutoff,),
    )
    return cursor.rowcount


def require_mutable(
    conn: sqlite3.Connection,
    project: str,
    *,
    allow_deleting: bool = False,
) -> None:
    """Reject a topology mutation while deletion or any lease is active."""
    cleanup_expired(conn)
    row = conn.execute("SELECT deleting FROM project_lease_state WHERE singleton = 1").fetchone()
    if row is not None and bool(row[0]) and not allow_deleting:
        raise ProjectError(f"project '{project}' is being deleted")
    pairs = _active_pairs(conn)
    if pairs:
        rendered = ", ".join(f"{holder}:{lease_id}" for holder, lease_id in pairs)
        raise ProjectError(f"project '{project}' has active lease(s): {rendered}")


def acquire(
    paths: Paths,
    project: str,
    holder: str,
    lease_id: str,
) -> ProjectLease:
    """Acquire or return a durable lease on the current healthy topology."""
    return _acquire(paths, project, holder, lease_id, _AcquireMode(_FINALIZED, None))


def acquire_pending(
    paths: Paths,
    project: str,
    holder: str,
    lease_id: str,
    *,
    now: int | None = None,
) -> ProjectLease:
    """Acquire a short-lived lease to protect topology during session creation."""
    acquired_at = int(time.time()) if now is None else now
    return _acquire(
        paths,
        project,
        holder,
        lease_id,
        mode=_AcquireMode(
            state=_PENDING,
            expires_at=acquired_at + PENDING_TTL_SECONDS,
            now=acquired_at,
        ),
    )


def _acquire(
    paths: Paths,
    project: str,
    holder: str,
    lease_id: str,
    mode: _AcquireMode,
) -> ProjectLease:
    holder = _key(holder, "holder")
    lease_id = _key(lease_id, "lease-id")
    db_path = discovery.require_project_db(paths, project)
    with db.transaction(db_path, immediate=True) as conn:
        cleanup_expired(conn, now=mode.now)
        deletion_state = conn.execute(
            "SELECT deleting FROM project_lease_state WHERE singleton = 1"
        ).fetchone()
        if deletion_state is not None and bool(deletion_state[0]):
            raise ProjectError(f"project '{project}' is being deleted")
        existing = conn.execute(
            "SELECT acquired_at, state, expires_at FROM project_leases "
            "WHERE holder = ? AND lease_id = ?",
            (holder, lease_id),
        ).fetchone()
        if existing is not None:
            return ProjectLease(
                project=project,
                path=paths.project(project),
                holder=holder,
                lease_id=lease_id,
                acquired_at=int(existing[0]),
                state=str(existing[1]),
                expires_at=int(existing[2]) if existing[2] is not None else None,
                worktrees=_stored_worktrees(paths, project, conn, holder, lease_id),
            )

        acquired_at = int(time.time()) if mode.now is None else mode.now
        worktrees = _healthy_worktrees(paths, project, conn)
        conn.execute(
            "INSERT INTO project_leases "
            "(holder, lease_id, acquired_at, state, expires_at) VALUES (?, ?, ?, ?, ?)",
            (holder, lease_id, acquired_at, mode.state, mode.expires_at),
        )
        conn.executemany(
            "INSERT INTO project_lease_worktrees "
            "(holder, lease_id, name, repo, slot_uuid) VALUES (?, ?, ?, ?, ?)",
            [
                (holder, lease_id, worktree.name, worktree.repo, worktree.slot_uuid)
                for worktree in worktrees
            ],
        )
        return ProjectLease(
            project=project,
            path=paths.project(project),
            holder=holder,
            lease_id=lease_id,
            acquired_at=acquired_at,
            state=mode.state,
            expires_at=mode.expires_at,
            worktrees=worktrees,
        )


def finalize(
    paths: Paths,
    project: str,
    holder: str,
    pending_id: str,
    session_id: str,
) -> ProjectLease:
    """Atomically replace a pending lease with its durable session id."""
    holder = _key(holder, "holder")
    pending_id = _key(pending_id, "pending-id")
    session_id = _key(session_id, "session-id")
    db_path = discovery.require_project_db(paths, project)
    with db.transaction(db_path, immediate=True) as conn:
        cleanup_expired(conn)
        finalized = conn.execute(
            "SELECT acquired_at, state, expires_at FROM project_leases "
            "WHERE holder = ? AND lease_id = ?",
            (holder, session_id),
        ).fetchone()
        if finalized is not None:
            if str(finalized[1]) != _FINALIZED:
                raise ProjectError(f"lease '{holder}:{session_id}' is still pending")
            conn.execute(
                "DELETE FROM project_leases "
                "WHERE holder = ? AND lease_id = ? AND state = 'pending'",
                (holder, pending_id),
            )
            return ProjectLease(
                project=project,
                path=paths.project(project),
                holder=holder,
                lease_id=session_id,
                acquired_at=int(finalized[0]),
                state=_FINALIZED,
                expires_at=None,
                worktrees=_stored_worktrees(paths, project, conn, holder, session_id),
            )

        pending = conn.execute(
            "SELECT acquired_at, state FROM project_leases WHERE holder = ? AND lease_id = ?",
            (holder, pending_id),
        ).fetchone()
        if pending is None or str(pending[1]) != _PENDING:
            raise ProjectError(f"pending lease '{holder}:{pending_id}' was not found")

        acquired_at = int(pending[0])
        conn.execute(
            "INSERT INTO project_leases "
            "(holder, lease_id, acquired_at, state, expires_at) "
            "VALUES (?, ?, ?, 'finalized', NULL)",
            (holder, session_id, acquired_at),
        )
        conn.execute(
            "INSERT INTO project_lease_worktrees (holder, lease_id, name, repo, slot_uuid) "
            "SELECT holder, ?, name, repo, slot_uuid FROM project_lease_worktrees "
            "WHERE holder = ? AND lease_id = ?",
            (session_id, holder, pending_id),
        )
        conn.execute(
            "DELETE FROM project_leases WHERE holder = ? AND lease_id = ?",
            (holder, pending_id),
        )
        return ProjectLease(
            project=project,
            path=paths.project(project),
            holder=holder,
            lease_id=session_id,
            acquired_at=acquired_at,
            state=_FINALIZED,
            expires_at=None,
            worktrees=_stored_worktrees(paths, project, conn, holder, session_id),
        )


def release(
    paths: Paths,
    project: str,
    holder: str,
    lease_id: str,
) -> LeaseRelease:
    """Idempotently release one project lease."""
    holder = _key(holder, "holder")
    lease_id = _key(lease_id, "lease-id")
    db_path = discovery.require_project_db(paths, project)
    with db.transaction(db_path, immediate=True) as conn:
        conn.execute(
            "DELETE FROM project_lease_worktrees WHERE holder = ? AND lease_id = ?",
            (holder, lease_id),
        )
        cursor = conn.execute(
            "DELETE FROM project_leases WHERE holder = ? AND lease_id = ?",
            (holder, lease_id),
        )
        return LeaseRelease(project, holder, lease_id, cursor.rowcount > 0)


def list_for_projects(
    paths: Paths,
    projects: list[str],
    *,
    holder: str | None = None,
) -> list[ProjectLease]:
    """List leases for concrete projects, optionally filtering by holder."""
    normalized_holder = _key(holder, "holder") if holder is not None else None
    leases: list[ProjectLease] = []
    for project in projects:
        db_path = discovery.require_project_db(paths, project)
        with db.transaction(db_path, immediate=True) as conn:
            cleanup_expired(conn)
            if normalized_holder is None:
                rows = conn.execute(
                    "SELECT holder, lease_id, acquired_at, state, expires_at "
                    "FROM project_leases ORDER BY holder, lease_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT holder, lease_id, acquired_at, state, expires_at "
                    "FROM project_leases "
                    "WHERE holder = ? ORDER BY lease_id",
                    (normalized_holder,),
                ).fetchall()
            leases.extend(
                ProjectLease(
                    project=project,
                    path=paths.project(project),
                    holder=row_holder,
                    lease_id=row_lease_id,
                    acquired_at=int(acquired_at),
                    state=str(state),
                    expires_at=int(expires_at) if expires_at is not None else None,
                    worktrees=_stored_worktrees(
                        paths,
                        project,
                        conn,
                        row_holder,
                        row_lease_id,
                    ),
                )
                for row_holder, row_lease_id, acquired_at, state, expires_at in rows
            )
    return leases


def release_for_holder(paths: Paths, holder: str, lease_id: str) -> list[LeaseRelease]:
    """Release a holder/id pair wherever it exists, without requiring a project name."""
    releases: list[LeaseRelease] = []
    for project, _db_path in discovery.list_project_dbs(paths):
        result = release(paths, project, holder, lease_id)
        if result.released:
            releases.append(result)
    return releases


def begin_delete(paths: Paths, project: str) -> None:
    """Atomically prevent new leases before whole-project deletion."""
    db_path = discovery.require_project_db(paths, project)
    with db.transaction(db_path, immediate=True) as conn:
        require_mutable(conn, project)
        conn.execute("UPDATE project_lease_state SET deleting = 1 WHERE singleton = 1")


def cancel_delete(paths: Paths, project: str) -> None:
    """Clear a failed whole-project deletion marker when its DB remains."""
    db_path = paths.project_db(project)
    if not db_path.is_file():
        return
    with db.transaction(db_path, immediate=True) as conn:
        conn.execute("UPDATE project_lease_state SET deleting = 0 WHERE singleton = 1")
