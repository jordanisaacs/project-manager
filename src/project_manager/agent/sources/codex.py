"""Codex CLI source: one query against `~/.codex/state_5.sqlite`.

Codex keeps an indexed `threads` table with `cwd`, `title`,
`first_user_message`, and `updated_at` (seconds) already materialized —
much cheaper than scanning rollout JSONL on disk. Open read-only via the
`file:?mode=ro` URI so concurrent usage with a running `codex` instance
is safe.
"""

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from project_manager.agent.sources._entry import SessionEntry

_AGENT = "codex"


async def fetch(owned_paths: set[Path], limit: int) -> list[SessionEntry]:
    """Return up to `limit` most-recent Codex sessions matching any owned cwd."""
    return await asyncio.to_thread(_fetch_sync, owned_paths, limit)


def _fetch_sync(owned_paths: set[Path], limit: int) -> list[SessionEntry]:
    db = Path.home() / ".codex" / "state_5.sqlite"
    if not db.is_file():
        return []
    if not owned_paths:
        return []
    cwd_strings = [str(p) for p in owned_paths]
    # `placeholders` is a fixed-form string of `?` literals sized to
    # the IN-list length; no user-supplied data enters the SQL text
    # itself. Values are bound positionally below.
    placeholders = ",".join("?" * len(cwd_strings))
    query = _build_query(placeholders)
    uri = f"file:{db}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(query, [*cwd_strings, limit]).fetchall()
    finally:
        conn.close()
    return [
        SessionEntry(
            agent=_AGENT,
            session_id=str(row[0]),
            title=_pick_title(row[1], row[2]),
            last_active=datetime.fromtimestamp(int(row[4]), tz=UTC),
            cwd=Path(str(row[3])),
        )
        for row in rows
    ]


_SELECT_SQL = (
    "SELECT id, title, first_user_message, cwd, updated_at FROM threads "
    "WHERE archived = 0 AND cwd IN ({}) "
    "AND (title != '' OR first_user_message != '') "
    "ORDER BY updated_at DESC LIMIT ?"
)


def _build_query(placeholders: str) -> str:
    # `placeholders` is always a fixed string of `?` literals sized to
    # the IN-list length — never user input, so `.format()` is safe here.
    return _SELECT_SQL.format(placeholders)


def _pick_title(title: str | None, first_user_message: str | None) -> str:
    # `title` is often empty for older sessions; fall back to the first
    # user prompt, which Codex stores verbatim in its own column.
    if title:
        collapsed = " ".join(title.split())
        if collapsed:
            return collapsed
    if first_user_message:
        collapsed = " ".join(first_user_message.split())
        if collapsed:
            return collapsed
    return "<no summary>"
