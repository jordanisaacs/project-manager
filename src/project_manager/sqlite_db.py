"""Package-level SQLite helpers.

Two context managers: `transaction()` wraps a unit of work with commit/rollback,
`readonly()` is a connection-only CM for reads. Both take an optional `schema`
string that is executed (idempotent `CREATE TABLE IF NOT EXISTS ...`) and
committed before the caller's block runs, so a rollback can't drop the tables
on first use.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _apply_pragmas(
    conn: sqlite3.Connection,
    *,
    wal: bool,
    busy_timeout_ms: int | None,
) -> None:
    """Apply opt-in connection PRAGMAs.

    `busy_timeout` is set first so a concurrent writer is waited on rather
    than failing fast with SQLITE_BUSY. `wal` enables WAL journaling — the
    right mode when a long-lived writer (the `pm agent serve` daemon) and
    external readers share the file, since readers then get a non-blocking
    consistent snapshot. Both values are ints / a literal mode name, never
    user input, so the f-string is safe.
    """
    if busy_timeout_ms is not None:
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")


@contextmanager
def transaction(
    db_path: Path,
    *,
    schema: str = "",
    row_factory: type | None = None,
    wal: bool = False,
    busy_timeout_ms: int | None = None,
) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    _apply_pragmas(conn, wal=wal, busy_timeout_ms=busy_timeout_ms)
    if row_factory is not None:
        conn.row_factory = row_factory
    try:
        if schema:
            conn.executescript(schema)
            conn.commit()
        with conn:
            yield conn
    finally:
        conn.close()


@contextmanager
def readonly(
    db_path: Path,
    *,
    schema: str = "",
    row_factory: type | None = None,
    wal: bool = False,
    busy_timeout_ms: int | None = None,
) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    _apply_pragmas(conn, wal=wal, busy_timeout_ms=busy_timeout_ms)
    if row_factory is not None:
        conn.row_factory = row_factory
    try:
        if schema:
            conn.executescript(schema)
            conn.commit()
        yield conn
    finally:
        conn.close()
