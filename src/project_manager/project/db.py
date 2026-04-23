"""Per-project SQLite state.

Each project has `~/.projects/<name>/.pm.db` with a single table `repos(name, slot_uuid)`.
The db is the source of truth for "what repos belong to this project"; the filesystem
symlinks materialize "currently attached" on top.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from project_manager import sqlite_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS repos (
    name TEXT PRIMARY KEY,
    slot_uuid TEXT NOT NULL
)
"""


@contextmanager
def transaction(db_path: Path) -> Iterator[sqlite3.Connection]:
    with sqlite_db.transaction(db_path, schema=_SCHEMA) as conn:
        yield conn


@contextmanager
def readonly(db_path: Path) -> Iterator[sqlite3.Connection]:
    with sqlite_db.readonly(db_path, schema=_SCHEMA) as conn:
        yield conn


def add_repo(conn: sqlite3.Connection, name: str, slot_uuid: str) -> None:
    conn.execute("INSERT INTO repos (name, slot_uuid) VALUES (?, ?)", (name, slot_uuid))


def update_slot(conn: sqlite3.Connection, name: str, slot_uuid: str) -> None:
    conn.execute("UPDATE repos SET slot_uuid = ? WHERE name = ?", (slot_uuid, name))


def remove_repo(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("DELETE FROM repos WHERE name = ?", (name,))


def get_slot(conn: sqlite3.Connection, name: str) -> str | None:
    row = conn.execute("SELECT slot_uuid FROM repos WHERE name = ?", (name,)).fetchone()
    return row[0] if row is not None else None


def list_repos(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return [
        (name, slot_uuid)
        for (name, slot_uuid) in conn.execute(
            "SELECT name, slot_uuid FROM repos ORDER BY name"
        ).fetchall()
    ]
