"""Durable local identity map for idempotent Omnigent synchronization."""

from __future__ import annotations

import builtins
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from project_manager import sqlite_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS omnigent_project_sync (
    server_url   TEXT NOT NULL,
    project_name TEXT NOT NULL,
    remote_id    TEXT NOT NULL,
    PRIMARY KEY (server_url, project_name),
    UNIQUE (server_url, remote_id)
);
"""


@dataclass(frozen=True)
class Mapping:
    project_name: str
    remote_id: str


class SyncState:
    def __init__(self, path: Path) -> None:
        self._path = path

    def list(self, server_url: str) -> builtins.list[Mapping]:
        with sqlite_db.readonly(self._path, schema=_SCHEMA, row_factory=sqlite3.Row) as conn:
            rows = conn.execute(
                "SELECT project_name, remote_id FROM omnigent_project_sync "
                "WHERE server_url = ? ORDER BY project_name",
                (server_url,),
            ).fetchall()
        return [
            Mapping(project_name=row["project_name"], remote_id=row["remote_id"]) for row in rows
        ]

    def put(self, server_url: str, project_name: str, remote_id: str) -> None:
        with sqlite_db.transaction(self._path, schema=_SCHEMA) as conn:
            conn.execute(
                "INSERT INTO omnigent_project_sync (server_url, project_name, remote_id) "
                "VALUES (?, ?, ?) ON CONFLICT (server_url, project_name) "
                "DO UPDATE SET remote_id = excluded.remote_id",
                (server_url, project_name, remote_id),
            )

    def remove(self, server_url: str, project_name: str) -> None:
        with sqlite_db.transaction(self._path, schema=_SCHEMA) as conn:
            conn.execute(
                "DELETE FROM omnigent_project_sync WHERE server_url = ? AND project_name = ?",
                (server_url, project_name),
            )
