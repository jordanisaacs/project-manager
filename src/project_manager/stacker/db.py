from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from project_manager import sqlite_db

from .models import OperationState, TrackedBranch

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked_branches (
    repo_name                 TEXT NOT NULL,
    branch                    TEXT NOT NULL,
    parent_repo_name          TEXT NOT NULL,
    parent_branch             TEXT NOT NULL,
    managed_base_commit       TEXT NOT NULL,
    last_synced_parent_commit TEXT,
    last_clean_head           TEXT,
    pr_url                    TEXT,
    updated_at                TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo_name, branch)
);

CREATE TABLE IF NOT EXISTS operations (
    repo_name          TEXT PRIMARY KEY,
    op_type            TEXT NOT NULL,
    status             TEXT NOT NULL,
    branch             TEXT,
    parent_branch      TEXT,
    root_branch        TEXT,
    queue_json         TEXT,
    current_index      INTEGER NOT NULL DEFAULT 0,
    start_head         TEXT,
    target_parent_head TEXT,
    commit_list_json   TEXT,
    next_commit_index  INTEGER NOT NULL DEFAULT 0,
    error_message      TEXT,
    updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS config (
    repo_name  TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo_name, key)
);
"""


class StackerDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with sqlite_db.transaction(self.db_path, schema=_SCHEMA, row_factory=sqlite3.Row) as conn:
            _ensure_tracked_branches_pr_url(conn)
            yield conn

    def upsert_branch(self, tracked: TrackedBranch) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tracked_branches (
                    repo_name, branch,
                    parent_repo_name, parent_branch,
                    managed_base_commit, last_synced_parent_commit, last_clean_head,
                    pr_url, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(repo_name, branch) DO UPDATE SET
                    parent_repo_name = excluded.parent_repo_name,
                    parent_branch = excluded.parent_branch,
                    managed_base_commit = excluded.managed_base_commit,
                    last_synced_parent_commit = excluded.last_synced_parent_commit,
                    last_clean_head = excluded.last_clean_head,
                    pr_url = excluded.pr_url,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    tracked.repo_name,
                    tracked.branch,
                    tracked.parent_repo_name,
                    tracked.parent_branch,
                    tracked.managed_base_commit,
                    tracked.last_synced_parent_commit,
                    tracked.last_clean_head,
                    tracked.pr_url,
                ),
            )

    def get_branch(self, repo_name: str, branch: str) -> TrackedBranch | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM tracked_branches WHERE repo_name = ? AND branch = ?",
                (repo_name, branch),
            ).fetchone()
        return _row_to_branch(row) if row else None

    def list_branches(self, repo_name: str | None = None) -> list[TrackedBranch]:
        query = "SELECT * FROM tracked_branches"
        params: tuple[str, ...] = ()
        if repo_name:
            query += " WHERE repo_name = ?"
            params = (repo_name,)
        query += " ORDER BY repo_name, branch"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_branch(row) for row in rows]

    def get_children(self, repo_name: str, parent_branch: str) -> list[TrackedBranch]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tracked_branches
                WHERE repo_name = ? AND parent_repo_name = ? AND parent_branch = ?
                ORDER BY branch
                """,
                (repo_name, repo_name, parent_branch),
            ).fetchall()
        return [_row_to_branch(row) for row in rows]

    def get_config(self, repo_name: str, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM config WHERE repo_name = ? AND key = ?",
                (repo_name, key),
            ).fetchone()
        return row["value"] if row else None

    def set_config(self, repo_name: str, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO config (repo_name, key, value, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(repo_name, key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (repo_name, key, value),
            )

    def unset_config(self, repo_name: str, key: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM config WHERE repo_name = ? AND key = ?",
                (repo_name, key),
            )
            return cursor.rowcount > 0

    def list_config(self, repo_name: str) -> list[tuple[str, str]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT key, value FROM config WHERE repo_name = ? ORDER BY key",
                (repo_name,),
            ).fetchall()
        return [(row["key"], row["value"]) for row in rows]

    def get_operation(self, repo_name: str) -> OperationState | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE repo_name = ?",
                (repo_name,),
            ).fetchone()
        return _row_to_operation(row) if row else None

    def list_operations(self) -> list[OperationState]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM operations ORDER BY repo_name").fetchall()
        return [_row_to_operation(row) for row in rows]

    def put_operation(self, op: OperationState) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO operations (
                    repo_name, op_type, status, branch, parent_branch, root_branch,
                    queue_json, current_index, start_head, target_parent_head,
                    commit_list_json, next_commit_index, error_message, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(repo_name) DO UPDATE SET
                    op_type = excluded.op_type,
                    status = excluded.status,
                    branch = excluded.branch,
                    parent_branch = excluded.parent_branch,
                    root_branch = excluded.root_branch,
                    queue_json = excluded.queue_json,
                    current_index = excluded.current_index,
                    start_head = excluded.start_head,
                    target_parent_head = excluded.target_parent_head,
                    commit_list_json = excluded.commit_list_json,
                    next_commit_index = excluded.next_commit_index,
                    error_message = excluded.error_message,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    op.repo_name,
                    op.op_type,
                    op.status,
                    op.branch,
                    op.parent_branch,
                    op.root_branch,
                    json.dumps(op.queue),
                    op.current_index,
                    op.start_head,
                    op.target_parent_head,
                    json.dumps(op.commit_list),
                    op.next_commit_index,
                    op.error_message,
                ),
            )

    def clear_operation(self, repo_name: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM operations WHERE repo_name = ?", (repo_name,))

    def delete_branch(self, repo_name: str, branch: str) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM tracked_branches WHERE repo_name = ? AND branch = ?",
                (repo_name, branch),
            )
            return cursor.rowcount


def _row_to_branch(row: sqlite3.Row) -> TrackedBranch:
    return TrackedBranch(
        repo_name=row["repo_name"],
        branch=row["branch"],
        parent_repo_name=row["parent_repo_name"],
        parent_branch=row["parent_branch"],
        managed_base_commit=row["managed_base_commit"],
        last_synced_parent_commit=row["last_synced_parent_commit"],
        last_clean_head=row["last_clean_head"],
        pr_url=row["pr_url"] if "pr_url" in row.keys() else None,  # noqa: SIM118 (sqlite3.Row has no `in`)
    )


def _ensure_tracked_branches_pr_url(conn: sqlite3.Connection) -> None:
    """Add the pr_url column to old DBs that predate it.

    `CREATE TABLE IF NOT EXISTS` skips when the table already exists, even
    if the schema diverges — so for a pre-existing DB we need an explicit
    ALTER. Gated on `PRAGMA table_info` so it's a no-op after the first run.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tracked_branches)")}
    if "pr_url" not in cols:
        conn.execute("ALTER TABLE tracked_branches ADD COLUMN pr_url TEXT")


def _row_to_operation(row: sqlite3.Row) -> OperationState:
    return OperationState(
        repo_name=row["repo_name"],
        op_type=row["op_type"],
        status=row["status"],
        branch=row["branch"],
        parent_branch=row["parent_branch"],
        root_branch=row["root_branch"],
        queue=json.loads(row["queue_json"]) if row["queue_json"] else [],
        current_index=row["current_index"],
        start_head=row["start_head"],
        target_parent_head=row["target_parent_head"],
        commit_list=json.loads(row["commit_list_json"]) if row["commit_list_json"] else [],
        next_commit_index=row["next_commit_index"],
        error_message=row["error_message"],
    )


