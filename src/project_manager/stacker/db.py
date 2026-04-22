from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import OperationState, RepoPRConfig, TrackedWorktree


class StackerDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS worktrees (
                    repo_root TEXT NOT NULL,
                    worktree_path TEXT PRIMARY KEY,
                    branch TEXT NOT NULL,
                    parent_repo_root TEXT NOT NULL,
                    parent_worktree_path TEXT NOT NULL,
                    parent_branch TEXT NOT NULL,
                    managed_base_commit TEXT NOT NULL,
                    last_synced_parent_commit TEXT,
                    last_clean_head TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(repo_root, branch)
                );

                CREATE TABLE IF NOT EXISTS operations (
                    repo_root TEXT PRIMARY KEY,
                    op_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    root_path TEXT,
                    root_branch TEXT,
                    queue_json TEXT,
                    current_index INTEGER NOT NULL DEFAULT 0,
                    worktree_path TEXT,
                    worktree_branch TEXT,
                    parent_path TEXT,
                    parent_branch TEXT,
                    start_head TEXT,
                    target_parent_head TEXT,
                    commit_list_json TEXT,
                    next_commit_index INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS repo_pr_config (
                    repo_root TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    trunk_branch TEXT NOT NULL,
                    main_repo TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    def upsert_worktree(self, worktree: TrackedWorktree) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO worktrees (
                    repo_root, worktree_path, branch,
                    parent_repo_root, parent_worktree_path, parent_branch,
                    managed_base_commit, last_synced_parent_commit, last_clean_head, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(worktree_path) DO UPDATE SET
                    repo_root = excluded.repo_root,
                    branch = excluded.branch,
                    parent_repo_root = excluded.parent_repo_root,
                    parent_worktree_path = excluded.parent_worktree_path,
                    parent_branch = excluded.parent_branch,
                    managed_base_commit = excluded.managed_base_commit,
                    last_synced_parent_commit = excluded.last_synced_parent_commit,
                    last_clean_head = excluded.last_clean_head,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    worktree.repo_root,
                    worktree.worktree_path,
                    worktree.branch,
                    worktree.parent_repo_root,
                    worktree.parent_worktree_path,
                    worktree.parent_branch,
                    worktree.managed_base_commit,
                    worktree.last_synced_parent_commit,
                    worktree.last_clean_head,
                ),
            )

    def get_worktree(self, repo_root: str, worktree_path: str, branch: str) -> TrackedWorktree | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM worktrees
                WHERE repo_root = ? AND worktree_path = ? AND branch = ?
                """,
                (repo_root, worktree_path, branch),
            ).fetchone()
        return _row_to_worktree(row) if row else None

    def get_worktree_by_branch(self, repo_root: str, branch: str) -> TrackedWorktree | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM worktrees WHERE repo_root = ? AND branch = ?",
                (repo_root, branch),
            ).fetchone()
        return _row_to_worktree(row) if row else None

    def list_worktrees(self, repo_root: str | None = None) -> list[TrackedWorktree]:
        query = "SELECT * FROM worktrees"
        params: tuple[str, ...] = ()
        if repo_root:
            query += " WHERE repo_root = ?"
            params = (repo_root,)
        query += " ORDER BY repo_root, branch"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_row_to_worktree(row) for row in rows]

    def get_children(self, repo_root: str, parent_path: str, parent_branch: str) -> list[TrackedWorktree]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM worktrees
                WHERE repo_root = ? AND parent_worktree_path = ? AND parent_branch = ?
                ORDER BY branch
                """,
                (repo_root, parent_path, parent_branch),
            ).fetchall()
        return [_row_to_worktree(row) for row in rows]

    def upsert_repo_pr_config(self, config: RepoPRConfig) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO repo_pr_config (
                    repo_root, mode, trunk_branch, main_repo, updated_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(repo_root) DO UPDATE SET
                    mode = excluded.mode,
                    trunk_branch = excluded.trunk_branch,
                    main_repo = excluded.main_repo,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    config.repo_root,
                    config.mode,
                    config.trunk_branch,
                    config.main_repo,
                ),
            )

    def get_repo_pr_config(self, repo_root: str) -> RepoPRConfig | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM repo_pr_config WHERE repo_root = ?",
                (repo_root,),
            ).fetchone()
        return _row_to_repo_pr_config(row) if row else None

    def get_operation(self, repo_root: str) -> OperationState | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM operations WHERE repo_root = ?",
                (repo_root,),
            ).fetchone()
        return _row_to_operation(row) if row else None

    def put_operation(self, op: OperationState) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO operations (
                    repo_root, op_type, status, root_path, root_branch,
                    queue_json, current_index, worktree_path, worktree_branch,
                    parent_path, parent_branch, start_head, target_parent_head,
                    commit_list_json, next_commit_index, error_message, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(repo_root) DO UPDATE SET
                    op_type = excluded.op_type,
                    status = excluded.status,
                    root_path = excluded.root_path,
                    root_branch = excluded.root_branch,
                    queue_json = excluded.queue_json,
                    current_index = excluded.current_index,
                    worktree_path = excluded.worktree_path,
                    worktree_branch = excluded.worktree_branch,
                    parent_path = excluded.parent_path,
                    parent_branch = excluded.parent_branch,
                    start_head = excluded.start_head,
                    target_parent_head = excluded.target_parent_head,
                    commit_list_json = excluded.commit_list_json,
                    next_commit_index = excluded.next_commit_index,
                    error_message = excluded.error_message,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    op.repo_root,
                    op.op_type,
                    op.status,
                    op.root_path,
                    op.root_branch,
                    json.dumps(op.queue),
                    op.current_index,
                    op.worktree_path,
                    op.worktree_branch,
                    op.parent_path,
                    op.parent_branch,
                    op.start_head,
                    op.target_parent_head,
                    json.dumps(op.commit_list),
                    op.next_commit_index,
                    op.error_message,
                ),
            )

    def clear_operation(self, repo_root: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM operations WHERE repo_root = ?", (repo_root,))

    def delete_worktree(self, repo_root: str, worktree_path: str, branch: str) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                """
                DELETE FROM worktrees
                WHERE repo_root = ? AND worktree_path = ? AND branch = ?
                """,
                (repo_root, worktree_path, branch),
            )
            return cursor.rowcount


def _row_to_worktree(row: sqlite3.Row) -> TrackedWorktree:
    return TrackedWorktree(
        repo_root=row["repo_root"],
        worktree_path=row["worktree_path"],
        branch=row["branch"],
        parent_repo_root=row["parent_repo_root"],
        parent_worktree_path=row["parent_worktree_path"],
        parent_branch=row["parent_branch"],
        managed_base_commit=row["managed_base_commit"],
        last_synced_parent_commit=row["last_synced_parent_commit"],
        last_clean_head=row["last_clean_head"],
    )


def _row_to_operation(row: sqlite3.Row) -> OperationState:
    return OperationState(
        repo_root=row["repo_root"],
        op_type=row["op_type"],
        status=row["status"],
        root_path=row["root_path"],
        root_branch=row["root_branch"],
        queue=json.loads(row["queue_json"]) if row["queue_json"] else [],
        current_index=row["current_index"],
        worktree_path=row["worktree_path"],
        worktree_branch=row["worktree_branch"],
        parent_path=row["parent_path"],
        parent_branch=row["parent_branch"],
        start_head=row["start_head"],
        target_parent_head=row["target_parent_head"],
        commit_list=json.loads(row["commit_list_json"]) if row["commit_list_json"] else [],
        next_commit_index=row["next_commit_index"],
        error_message=row["error_message"],
    )


def _row_to_repo_pr_config(row: sqlite3.Row) -> RepoPRConfig:
    return RepoPRConfig(
        repo_root=row["repo_root"],
        mode=row["mode"],
        trunk_branch=row["trunk_branch"],
        main_repo=row["main_repo"],
    )
