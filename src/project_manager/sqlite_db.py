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


@contextmanager
def transaction(
    db_path: Path,
    *,
    schema: str = "",
    row_factory: type | None = None,
) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
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
) -> Iterator[sqlite3.Connection]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    if row_factory is not None:
        conn.row_factory = row_factory
    try:
        if schema:
            conn.executescript(schema)
            conn.commit()
        yield conn
    finally:
        conn.close()
