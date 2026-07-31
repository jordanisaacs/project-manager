"""WAL SQLite session store for `pm serve`.

Holds one row per live session plus an append-only `events` log. The DB
is ephemeral — recreated on each serve start (`reset=True`) — so it is
working state for the lifetime of the daemon, not durable history. WAL +
`busy_timeout` let concurrent hook writers and external readers share the
file without blocking.

Each method opens a short-lived connection via `sqlite_db`, so the store
is safe to call from any thread of the threaded HTTP server.
"""

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from project_manager import sqlite_db
from project_manager.agent.serve import status as status_mod
from project_manager.paths import Paths

_BUSY_TIMEOUT_MS = 3000

# Session ids we refuse to create rows from: a hook may fire (e.g.
# SessionStart) before the agent has minted its real id, reporting an
# empty/placeholder value. Creating a row for it would orphan a phantom
# session that the real id never reconciles with (db-agents' lesson).
_PLACEHOLDER_IDS = frozenset({"", "-", "unknown", "null", "none", "pending"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    agent             TEXT    NOT NULL,
    vendor_session_id TEXT    NOT NULL,
    cwd               TEXT,
    project           TEXT,
    title             TEXT,
    status            TEXT    NOT NULL,
    activity          TEXT,
    current_tool      TEXT,
    model             TEXT,
    tokens            INTEGER,
    last_activity_at  INTEGER,
    updated_at        INTEGER NOT NULL,
    source            TEXT    NOT NULL,
    meta              TEXT    NOT NULL DEFAULT '{}',
    transcript_path   TEXT,
    pid               INTEGER,
    PRIMARY KEY (agent, vendor_session_id)
);
CREATE TABLE IF NOT EXISTS events (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    agent             TEXT,
    vendor_session_id TEXT,
    event             TEXT,
    payload           TEXT,
    at                INTEGER
);
"""

_COLUMNS = (
    "agent",
    "vendor_session_id",
    "cwd",
    "project",
    "title",
    "status",
    "activity",
    "current_tool",
    "model",
    "tokens",
    "last_activity_at",
    "updated_at",
    "source",
    "meta",
    "transcript_path",
    "pid",
)

# SQL is kept as plain string literals (no f-string/format interpolation) so the
# column list stays in lockstep with `_COLUMNS`; the guard below fails fast if
# the two ever drift. Reads use `SELECT *` + a row factory, so they need no
# column list at all.
_INSERT_SQL = (
    "INSERT INTO sessions "
    "(agent, vendor_session_id, cwd, project, title, status, activity, "
    "current_tool, model, tokens, last_activity_at, updated_at, source, meta, "
    "transcript_path, pid) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(agent, vendor_session_id) DO UPDATE SET "
    "cwd=excluded.cwd, project=excluded.project, title=excluded.title, "
    "status=excluded.status, activity=excluded.activity, "
    "current_tool=excluded.current_tool, model=excluded.model, "
    "tokens=excluded.tokens, last_activity_at=excluded.last_activity_at, "
    "updated_at=excluded.updated_at, source=excluded.source, meta=excluded.meta, "
    "transcript_path=excluded.transcript_path, pid=excluded.pid"
)
_EVENTS_INSERT_SQL = (
    "INSERT INTO events (agent, vendor_session_id, event, payload, at) VALUES (?, ?, ?, ?, ?)"
)
if _INSERT_SQL.count("?") != len(_COLUMNS):  # pragma: no cover - import-time invariant
    msg = "store._INSERT_SQL placeholder count drifted from _COLUMNS"
    raise RuntimeError(msg)

Session = dict[str, Any]


@dataclass(frozen=True)
class FallbackReading:
    """One Tier-2 status reading for a session (see `Store.apply_fallback`).

    Status only — titles are owned by `Store.set_title`, driven separately by
    the transcript-mtime pass in `fallback.py`."""

    agent: str
    session_id: str
    cwd: str | None
    project: str | None
    status: str
    last_activity_at: int


def _now_ms() -> int:
    return int(time.time() * 1000)


def is_placeholder_id(session_id: str | None) -> bool:
    """True for empty / sentinel session ids we must not persist."""
    return session_id is None or session_id.strip().lower() in _PLACEHOLDER_IDS


class Store:
    """Thread-safe (connection-per-call) WAL store of live sessions."""

    def __init__(self, db_path: Path, *, reset: bool = False) -> None:
        self.db_path = db_path
        if reset:
            self._wipe()
        with sqlite_db.transaction(
            db_path,
            schema=_SCHEMA,
            wal=True,
            busy_timeout_ms=_BUSY_TIMEOUT_MS,
        ):
            pass

    def _wipe(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.db_path) + suffix)
            p.unlink(missing_ok=True)

    # -- reads ----------------------------------------------------------------

    def get(self, agent: str, session_id: str) -> Session | None:
        with sqlite_db.readonly(
            self.db_path,
            busy_timeout_ms=_BUSY_TIMEOUT_MS,
            row_factory=sqlite3.Row,
        ) as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE agent = ? AND vendor_session_id = ?",
                (agent, session_id),
            ).fetchone()
        return _row_to_session(row) if row is not None else None

    def snapshot(self, filters: dict[str, str] | None = None) -> list[Session]:
        """Return all sessions matching `filters` (most-recent first).

        All filtering — first-class `agent`/`project`/`status` and `meta.<KEY>`
        — is applied in Python via `session_matches`; the row set is small
        (live sessions only), so a full scan is cheap and keeps the SQL a plain
        literal. Absent filters → every session.
        """
        with sqlite_db.readonly(
            self.db_path,
            busy_timeout_ms=_BUSY_TIMEOUT_MS,
            row_factory=sqlite3.Row,
        ) as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY last_activity_at DESC, updated_at DESC",
            ).fetchall()
        sessions = [_row_to_session(r) for r in rows]
        if not filters:
            return sessions
        return [s for s in sessions if session_matches(s, filters)]

    # -- writes ---------------------------------------------------------------

    def ingest(
        self, event: dict[str, Any], paths: Paths, now_ms: int | None = None
    ) -> Session | None:
        """Apply a hook event (Tier 1). Returns the updated session, or None.

        None means the event was rejected — a placeholder/empty session id,
        which we refuse to persist. A late-arriving real id simply upserts
        onto whatever row already exists for `(agent, session_id)`.
        """
        now = _now_ms() if now_ms is None else now_ms
        agent = str(event.get("agent") or "").strip()
        session_id = event.get("session_id")
        if not agent or is_placeholder_id(session_id):
            return None
        assert session_id is not None  # narrowed by is_placeholder_id

        report_status = event.get("status")
        hook_event = event.get("hook_event_name")
        tool = event.get("tool_name") or None

        prev = self.get(agent, session_id)
        new_status = status_mod.status_for(report_status)
        status = (
            new_status
            if new_status is not None
            else (prev["status"] if prev else status_mod.UNKNOWN)
        )
        activity = status_mod.activity_for(report_status, tool) or (
            prev["activity"] if prev else None
        )
        prev_last = prev["last_activity_at"] if prev and prev["last_activity_at"] else 0
        last_activity_at = status_mod.bump_last_activity_if_activity(prev_last, hook_event, now)

        cwd = event.get("cwd") or (prev["cwd"] if prev else None)
        project = status_mod.cwd_to_project(paths, cwd) or (prev["project"] if prev else None)
        meta = dict(prev["meta"]) if prev else {}
        if isinstance(event.get("meta"), dict):
            meta.update(event["meta"])

        session: Session = {
            "agent": agent,
            "vendor_session_id": session_id,
            "cwd": cwd,
            "project": project,
            "title": prev["title"] if prev else None,
            "status": status,
            "activity": activity,
            "current_tool": tool or (prev["current_tool"] if prev else None),
            "model": event.get("model") or (prev["model"] if prev else None),
            "tokens": event.get("tokens")
            if event.get("tokens") is not None
            else (prev["tokens"] if prev else None),
            "last_activity_at": last_activity_at,
            "updated_at": now,
            "source": "hook",
            "meta": meta,
            "transcript_path": event.get("transcript_path")
            or (prev["transcript_path"] if prev else None),
            "pid": event.get("pid") or (prev["pid"] if prev else None),
        }
        self._upsert(session, event=hook_event, payload=event, at=now)
        return session

    def apply_fallback(
        self,
        reading: FallbackReading,
        *,
        stale_ms: int,
        now_ms: int | None = None,
    ) -> Session | None:
        """Apply a Tier-2 status reading. Returns the row if it changed, else None.

        Never overrides a fresh hook signal: if an existing row came from a hook
        and was updated within `stale_ms`, the live status/activity stay (returns
        None). Titles are not touched here — see `set_title`.

        Also never *downgrades* a known status to `unknown`: the fallback can't
        classify Cursor/Codex, so a hooked session that simply goes quiet (e.g.
        idle, or waiting on the user to approve) would otherwise be clobbered to
        `unknown` once its last hook aged past `stale_ms`. When the reading is
        `unknown` and we already know a status, leave the row untouched.
        """
        now = _now_ms() if now_ms is None else now_ms
        if is_placeholder_id(reading.session_id):
            return None
        prev = self.get(reading.agent, reading.session_id)
        if prev is not None and prev["source"] == "hook" and now - prev["updated_at"] < stale_ms:
            return None
        if (
            reading.status == status_mod.UNKNOWN
            and prev is not None
            and prev["status"] != status_mod.UNKNOWN
        ):
            return None
        session: Session = {
            "agent": reading.agent,
            "vendor_session_id": reading.session_id,
            "cwd": reading.cwd or (prev["cwd"] if prev else None),
            "project": reading.project or (prev["project"] if prev else None),
            "title": prev["title"] if prev else None,
            "status": reading.status,
            "activity": reading.status,
            "current_tool": None,
            "model": prev["model"] if prev else None,
            "tokens": prev["tokens"] if prev else None,
            "last_activity_at": max(reading.last_activity_at, prev["last_activity_at"] or 0)
            if prev
            else reading.last_activity_at,
            "updated_at": now,
            "source": "fallback",
            "meta": dict(prev["meta"]) if prev else {},
            "transcript_path": prev["transcript_path"] if prev else None,
            "pid": prev["pid"] if prev else None,
        }
        self._upsert(session, event="fallback", payload=None, at=now)
        return session

    def delete(self, agent: str, session_id: str) -> bool:
        """Remove a session row. Returns True if a row was actually deleted."""
        with sqlite_db.transaction(self.db_path, busy_timeout_ms=_BUSY_TIMEOUT_MS) as conn:
            cur = conn.execute(
                "DELETE FROM sessions WHERE agent = ? AND vendor_session_id = ?",
                (agent, session_id),
            )
            return cur.rowcount > 0

    def set_title(self, agent: str, session_id: str, title: str) -> Session | None:
        """Set a session's title (e.g. derived from its transcript), if changed.

        Touches only `title`; status/source/timestamps are left intact. Returns
        the updated row, or None if the session is unknown or already titled so.
        """
        prev = self.get(agent, session_id)
        if prev is None or not title or prev["title"] == title:
            return None
        session: Session = {**prev, "title": title}
        self._upsert(session, event="title", payload=None, at=prev["updated_at"])
        return session

    def _upsert(self, session: Session, *, event: str | None, payload: object, at: int) -> None:
        row = tuple(
            json.dumps(session["meta"], separators=(",", ":")) if c == "meta" else session[c]
            for c in _COLUMNS
        )
        with sqlite_db.transaction(self.db_path, busy_timeout_ms=_BUSY_TIMEOUT_MS) as conn:
            conn.execute(_INSERT_SQL, row)
            conn.execute(
                _EVENTS_INSERT_SQL,
                (
                    session["agent"],
                    session["vendor_session_id"],
                    event,
                    json.dumps(payload, separators=(",", ":")) if payload is not None else None,
                    at,
                ),
            )


def _row_to_session(row: sqlite3.Row) -> Session:
    # sqlite3.Row iterates values (not keys), so zip the names with the values.
    session: Session = dict(zip(row.keys(), tuple(row), strict=True))
    raw_meta = session.get("meta")
    session["meta"] = json.loads(raw_meta) if raw_meta else {}
    return session


def session_matches(session: Session, filters: dict[str, str]) -> bool:
    """True if `session` satisfies every filter (used for SSE delta routing).

    Mirrors `snapshot`'s filtering: first-class `agent`/`project`/`status`
    equality plus `meta.<KEY>` against the parsed meta. Unknown filter keys
    are ignored (so a stray query param can't silently drop everything).
    """
    for key, value in filters.items():
        if key.startswith("meta."):
            if str(session["meta"].get(key[len("meta.") :])) != value:
                return False
        elif key in ("agent", "project", "status") and str(session.get(key)) != value:
            return False
    return True
