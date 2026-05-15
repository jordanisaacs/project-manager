"""Cursor CLI source: join `~/.cursor/projects/` and `~/.config/cursor/chats/`.

Cursor splits a session's data across two directory trees:

- `~/.cursor/projects/<enc>/agent-transcripts/<session-uuid>/<session-uuid>.jsonl`
  lists which sessions belong to which cwd, and holds the transcript (whose
  mtime is the last-active time we want).

- `~/.config/cursor/chats/<workspace-id>/<session-uuid>/store.db` holds the
  session metadata (`name`, `createdAt`) as a hex-encoded JSON blob under
  `meta.value WHERE key='0'`. Not every transcript has a matching store.db
  (older sessions lack one) — fall back to the first user prompt from the
  transcript JSONL in that case.

Cwd encoding for `~/.cursor/projects/` (matches live data): replace any
run of `/` or `.` with a single `-`, then drop any leading `-`. So
`/home/alice/.projects/foo` becomes `home-alice-projects-foo` (single
dash for the `.projects` segment, not `--projects`).
"""

import asyncio
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from project_manager.agent.sources._entry import SessionEntry

_AGENT = "cursor"
_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
_PATH_SEP_RE = re.compile(r"[/.]+")


async def fetch(owned_paths: set[Path], limit: int) -> list[SessionEntry]:
    """Return up to `limit` most-recent Cursor sessions for the given cwds."""
    return await asyncio.to_thread(_fetch_sync, owned_paths, limit)


def _fetch_sync(owned_paths: set[Path], limit: int) -> list[SessionEntry]:
    projects_root = Path.home() / ".cursor" / "projects"
    if not projects_root.is_dir():
        return []
    candidates = _collect_candidates(projects_root, owned_paths)
    if not candidates:
        return []
    candidates.sort(key=lambda t: t[3], reverse=True)
    store_index = _build_store_index()
    # Walk candidates in mtime order; keep taking valid sessions until
    # we have `limit` or exhaust the list. Guarantees we don't drop a
    # newer valid session just because a cluster of empties came first.
    entries: list[SessionEntry] = []
    for cwd, session_id, transcript, mtime in candidates:
        title = _read_store_name(store_index.get(session_id)) or _first_user_query(transcript)
        if title is None:
            # No human-assigned name and no user prompt in the transcript
            # — the session is empty (bash-only or metadata-only). Drop
            # it from the listing instead of showing `<no summary>`.
            continue
        entries.append(
            SessionEntry(
                agent=_AGENT,
                session_id=session_id,
                title=title,
                last_active=datetime.fromtimestamp(mtime, tz=UTC),
                cwd=cwd,
            ),
        )
        if len(entries) >= limit:
            break
    return entries


def _collect_candidates(
    projects_root: Path,
    owned_paths: set[Path],
) -> list[tuple[Path, str, Path, float]]:
    out: list[tuple[Path, str, Path, float]] = []
    for cwd in owned_paths:
        transcripts = projects_root / _encode_path(cwd) / "agent-transcripts"
        if not transcripts.is_dir():
            continue
        for session_dir in transcripts.iterdir():
            if not session_dir.is_dir():
                continue
            session_id = session_dir.name
            transcript = session_dir / f"{session_id}.jsonl"
            if not transcript.is_file():
                continue
            try:
                mtime = transcript.stat().st_mtime
            except OSError:
                continue
            out.append((cwd, session_id, transcript, mtime))
    return out


def _encode_path(p: Path) -> str:
    return _PATH_SEP_RE.sub("-", str(p)).lstrip("-")


def _build_store_index() -> dict[str, Path]:
    """Map session_uuid → store.db path for every session under `chats/`.

    Walked once per `_fetch_sync` call. The chats tree is flat
    (`chats/<workspace>/<session>/store.db`) so this is a bounded walk,
    not a full-filesystem scan.
    """
    chats_root = Path.home() / ".config" / "cursor" / "chats"
    index: dict[str, Path] = {}
    if not chats_root.is_dir():
        return index
    for workspace_dir in chats_root.iterdir():
        if not workspace_dir.is_dir():
            continue
        for session_dir in workspace_dir.iterdir():
            if not session_dir.is_dir():
                continue
            db = session_dir / "store.db"
            if db.is_file():
                index[session_dir.name] = db
    return index


def _read_store_name(db_path: Path | None) -> str | None:
    """Decode the hex-encoded JSON meta blob and return the `name` field."""
    if db_path is None:
        return None
    row = _read_meta_row(db_path)
    if row is None:
        return None
    try:
        decoded = bytes.fromhex(str(row[0])).decode("utf-8")
        meta = json.loads(decoded)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    name = meta.get("name")
    if not isinstance(name, str):
        return None
    collapsed = " ".join(name.split())
    return collapsed or None


def _read_meta_row(db_path: Path) -> tuple[object, ...] | None:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        return conn.execute("SELECT value FROM meta WHERE key='0'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _first_user_query(transcript: Path) -> str | None:
    """Extract the first user prompt from a Cursor transcript JSONL.

    User events are `{"role": "user", "message": {"content": [{"type":
    "text", "text": "<user_query>...</user_query>"}]}}`. We strip the
    `<user_query>` wrapper Cursor injects so the title reads as natural
    prose, not XML.
    """
    try:
        with transcript.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("role") != "user":
                    continue
                text = _extract_text(obj.get("message", {}).get("content"))
                if not text:
                    continue
                m = _USER_QUERY_RE.search(text)
                prompt = m.group(1) if m else text
                collapsed = " ".join(prompt.split())
                if collapsed:
                    return collapsed
    except OSError:
        return None
    return None


def _extract_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            typed = cast("dict[str, object]", block)
            if typed.get("type") == "text":
                txt = typed.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
        return "\n".join(parts)
    return ""
