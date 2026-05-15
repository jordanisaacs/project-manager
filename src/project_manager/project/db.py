"""Per-project SQLite state.

Each project has `~/.projects/<name>/.pm.db` with one table
`worktrees(name, repo, slot_uuid, branch)`. `name` is the worktree label
(and the symlink name under the project dir); `repo` is the pool key.
Two worktrees may share a repo; their `name` must still be unique.

`branch` holds the last-seen branch to restore on re-attach (NULL when
none saved). Filesystem symlinks materialize "currently attached" on top
of these rows.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from project_manager import sqlite_db

_CREATE_WORKTREES = """
CREATE TABLE worktrees (
    name      TEXT PRIMARY KEY,
    repo      TEXT NOT NULL,
    slot_uuid TEXT NOT NULL,
    branch    TEXT
)
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Create/upgrade schema. Idempotent.

    Legacy `repos(name, slot_uuid, branch)` dbs are migrated in place by
    copying rows into `worktrees` with wt name = repo name (the old 1:1
    mapping). Fresh dbs just create `worktrees`.
    """
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    if "worktrees" in tables:
        return
    if "repos" in tables:
        conn.execute(_CREATE_WORKTREES)
        conn.execute(
            "INSERT INTO worktrees (name, repo, slot_uuid, branch) "
            "SELECT name, name, slot_uuid, branch FROM repos"
        )
        conn.execute("DROP TABLE repos")
    else:
        conn.execute(_CREATE_WORKTREES)
    conn.commit()


@contextmanager
def transaction(db_path: Path) -> Iterator[sqlite3.Connection]:
    with sqlite_db.transaction(db_path) as conn:
        _migrate(conn)
        yield conn


@contextmanager
def readonly(db_path: Path) -> Iterator[sqlite3.Connection]:
    with sqlite_db.readonly(db_path) as conn:
        _migrate(conn)
        yield conn


def add_wt(conn: sqlite3.Connection, wt: str, repo: str, slot_uuid: str) -> None:
    conn.execute(
        "INSERT INTO worktrees (name, repo, slot_uuid) VALUES (?, ?, ?)",
        (wt, repo, slot_uuid),
    )


def update_slot(conn: sqlite3.Connection, wt: str, slot_uuid: str) -> None:
    conn.execute("UPDATE worktrees SET slot_uuid = ? WHERE name = ?", (slot_uuid, wt))


def remove_wt(conn: sqlite3.Connection, wt: str) -> None:
    conn.execute("DELETE FROM worktrees WHERE name = ?", (wt,))


def get_slot(conn: sqlite3.Connection, wt: str) -> str | None:
    row = conn.execute("SELECT slot_uuid FROM worktrees WHERE name = ?", (wt,)).fetchone()
    return row[0] if row is not None else None


def get_repo(conn: sqlite3.Connection, wt: str) -> str | None:
    row = conn.execute("SELECT repo FROM worktrees WHERE name = ?", (wt,)).fetchone()
    return row[0] if row is not None else None


def get_branch(conn: sqlite3.Connection, wt: str) -> str | None:
    row = conn.execute("SELECT branch FROM worktrees WHERE name = ?", (wt,)).fetchone()
    return row[0] if row is not None else None


def set_branch(conn: sqlite3.Connection, wt: str, branch: str | None) -> None:
    conn.execute("UPDATE worktrees SET branch = ? WHERE name = ?", (branch, wt))


def list_wts(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    return [
        (name, repo, slot_uuid)
        for (name, repo, slot_uuid) in conn.execute(
            "SELECT name, repo, slot_uuid FROM worktrees ORDER BY name"
        ).fetchall()
    ]
