from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from project_manager import sqlite_db

from .slot import SlotBusyError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS slot_ownership (
    repo       TEXT NOT NULL,
    uuid       TEXT NOT NULL,
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('project', 'stacker')),
    owner_id   TEXT NOT NULL,
    claimed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (repo, uuid)
);
"""


class OwnerKind(StrEnum):
    PROJECT = "project"
    STACKER = "stacker"


@dataclass(frozen=True)
class Owner:
    kind: OwnerKind
    id: str


OWNER_STACKER_OPS = Owner(OwnerKind.STACKER, "ops")


class PoolDB:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with sqlite_db.transaction(self.db_path, schema=_SCHEMA, row_factory=sqlite3.Row) as conn:
            yield conn

    def claim(self, repo: str, uuid: str, owner: Owner) -> None:
        """Atomically claim (repo, uuid) for `owner`.

        Raises SlotBusyError if another claimer already owns this slot.
        """
        try:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO slot_ownership (repo, uuid, owner_kind, owner_id) "
                    "VALUES (?, ?, ?, ?)",
                    (repo, uuid, owner.kind.value, owner.id),
                )
        except sqlite3.IntegrityError as e:
            raise SlotBusyError(f"slot {repo}/{uuid} is already claimed") from e

    def release(self, repo: str, uuid: str) -> None:
        """Release (repo, uuid). Idempotent."""
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM slot_ownership WHERE repo = ? AND uuid = ?",
                (repo, uuid),
            )

    def get_owner(self, repo: str, uuid: str) -> Owner | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT owner_kind, owner_id FROM slot_ownership WHERE repo = ? AND uuid = ?",
                (repo, uuid),
            ).fetchone()
        if row is None:
            return None
        return Owner(kind=OwnerKind(row["owner_kind"]), id=row["owner_id"])

    def is_free(self, repo: str, uuid: str) -> bool:
        return self.get_owner(repo, uuid) is None

    def list_owned(self, repo: str | None = None) -> list[tuple[str, str, Owner]]:
        query = "SELECT repo, uuid, owner_kind, owner_id FROM slot_ownership"
        params: tuple[str, ...] = ()
        if repo is not None:
            query += " WHERE repo = ?"
            params = (repo,)
        query += " ORDER BY repo, uuid"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            (row["repo"], row["uuid"], Owner(kind=OwnerKind(row["owner_kind"]), id=row["owner_id"]))
            for row in rows
        ]
