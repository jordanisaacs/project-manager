"""Tier-2 fallback: derive session status from vendor transcripts.

Runs on a timer in the daemon. For every session `pm agent ls` can see,
it classifies status from the transcript tail and applies it to the store
*only* when no fresher hook signal exists (the store enforces that). This
is the safety net for sessions with no hook coverage (Cursor, or Claude/
Codex when a hook dropped or the daemon was down).

Critically — db-agents' lesson — status is derived only from *explicit*
transcript signals (`stop_reason`, last record type), never from file
mtime + an idle timer, which flickers. mtime feeds only "last active".

Claude's `.jsonl` is classified directly (port of tributary's
`jsonl_status.rs`). Codex/Cursor have no cheap explicit signal here, so
they register with status `unknown` until a hook arrives.
"""

import asyncio
import json
import os
import threading
from datetime import UTC
from pathlib import Path
from typing import cast

from project_manager.agent import scope, transcript
from project_manager.agent.serve import status as status_mod
from project_manager.agent.serve.server import SessionServer
from project_manager.agent.serve.store import FallbackReading
from project_manager.agent.sources import REGISTRY, SessionEntry
from project_manager.paths import Paths
from project_manager.project import discovery

# Stop reasons that mean the assistant finished its turn cleanly → idle.
# Anything else (notably `tool_use`) is ambiguous from the jsonl alone, so
# we stay on `working` and let a hook decide.
_IDLE_STOP_REASONS = frozenset({"end_turn", "stop_sequence", "max_tokens"})

# Per-project session cap for the fallback sweep, and the tail size read
# from each transcript. A session that hasn't touched its file recently
# won't be in the top-N anyway.
_FALLBACK_LIMIT = 50
_TAIL_BYTES = 32 * 1024

# A fallback reading older than this never masks a hook signal; the store
# also refuses to overwrite a hook row updated within the window.
_STALE_MS = 65 * 1000

# Source-reader titles that aren't real summaries — never propagate them.
_PLACEHOLDER_TITLES = frozenset({"", "<no summary>"})


def classify_last_record(text: str) -> str | None:
    """Classify a Claude transcript tail to working/idle (port of tributary).

    Returns None when there is no user/assistant record to judge.
    """
    last: dict[str, object] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") in ("user", "assistant"):
            last = obj
    if last is None:
        return None
    stop_reason = _stop_reason(last)
    if stop_reason is not None:
        return status_mod.IDLE if stop_reason in _IDLE_STOP_REASONS else status_mod.WORKING
    # No stop_reason: a trailing user prompt or a mid-stream assistant
    # record both mean the agent has work in flight.
    return status_mod.WORKING


def _stop_reason(record: dict[str, object]) -> str | None:
    message = record.get("message")
    if isinstance(message, dict):
        reason = cast("dict[str, object]", message).get("stop_reason")
        if isinstance(reason, str):
            return reason
    outer = record.get("stop_reason")
    return outer if isinstance(outer, str) else None


def _read_tail(path: Path, n: int) -> str | None:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - n))
            data = fh.read()
    except OSError:
        return None
    return data.decode("utf-8", errors="replace")


def _claude_transcript(cwd: Path, session_id: str) -> Path:
    # Claude's path encoding (see sources/claude.py): `/`→`-`, `.`→`-`.
    encoded = str(cwd).replace("/", "-").replace(".", "-")
    return Path.home() / ".claude" / "projects" / encoded / f"{session_id}.jsonl"


def classify_entry(entry: SessionEntry) -> str:
    """Best-effort explicit-signal status for one session; `unknown` if none."""
    if entry.agent == "claude":
        text = _read_tail(_claude_transcript(entry.cwd, entry.session_id), _TAIL_BYTES)
        if text is not None:
            return classify_last_record(text) or status_mod.UNKNOWN
    return status_mod.UNKNOWN


async def _collect(paths: Paths, projects: list[str], limit: int) -> list[tuple[str, SessionEntry]]:
    async with asyncio.TaskGroup() as tg:
        tasks = [
            (project, tg.create_task(fetch(scope.owned_paths(paths, project), limit)))
            for project in projects
            for fetch in REGISTRY.values()
        ]
    return [(project, entry) for project, task in tasks for entry in task.result()]


def poll_once(server: SessionServer) -> int:
    """Sweep every project's sessions once; return how many rows changed."""
    paths = server.paths
    projects = [name for name, _ in discovery.list_project_dbs(paths)]
    if not projects:
        return 0
    pairs = asyncio.run(_collect(paths, projects, _FALLBACK_LIMIT))
    changed = 0
    entry_titles: dict[tuple[str, str], str] = {}
    for project, entry in pairs:
        reading = FallbackReading(
            agent=entry.agent,
            session_id=entry.session_id,
            cwd=str(entry.cwd),
            project=project,
            status=classify_entry(entry),
            last_activity_at=int(entry.last_active.astimezone(UTC).timestamp() * 1000),
        )
        session = server.store.apply_fallback(reading, stale_ms=_STALE_MS)
        if session is not None:
            server.broadcaster.publish(session)
            changed += 1
        title = entry.title.strip()
        if title and title not in _PLACEHOLDER_TITLES:
            entry_titles[(entry.agent, entry.session_id)] = title
    changed += _refresh_titles(server)
    changed += _apply_source_reader_titles(server, entry_titles)
    changed += _prune_dead(server)
    return changed


def _apply_source_reader_titles(
    server: SessionServer, entry_titles: dict[tuple[str, str], str]
) -> int:
    """Title sessions that have no transcript title, from their source reader.

    Ownership is decided by whether the transcript pass actually produced a
    title (`server.transcript_titled`), not merely whether a `transcript_path`
    exists — Cursor *has* a transcript path but its format isn't parseable, so
    `_refresh_titles` yields nothing and Cursor is owned here. For these we
    apply the current `pm agent ls` title every poll, so a rename in the vendor
    store propagates; `set_title` is a no-op when unchanged, so steady state
    stays quiet.
    """
    changed = 0
    for s in server.store.snapshot():
        key = (s["agent"], s["vendor_session_id"])
        # Owned by the transcript title pass — don't fight it / flip-flop.
        if key in server.transcript_titled:
            continue
        title = entry_titles.get(key)
        if not title:
            continue
        updated = server.store.set_title(s["agent"], s["vendor_session_id"], title)
        if updated is not None:
            server.broadcaster.publish(updated)
            changed += 1
    return changed


def _pid_alive(pid: int) -> bool:
    """True if a process with `pid` still exists.

    `os.kill(pid, 0)` sends no signal but performs the existence + permission
    check: `ProcessLookupError` means gone, `PermissionError` means it exists
    under another uid (alive). pid <= 0 has special signal semantics, so treat
    those as "can't tell → alive" and never prune on them.
    """
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _prune_dead(server: SessionServer) -> int:
    """Backstop: drop sessions whose agent process is gone.

    The SessionEnd hook is the primary cleanup; this catches the case where it
    never fired (agent crash, hook drop, daemon was down at exit). The agent
    pid is the session-lifetime identifier — it exists for exactly as long as
    the session, unlike the terminal buffer, which can outlive it. Sessions
    with no recorded pid (e.g. fallback-discovered Cursor) are left alone.
    Returns how many rows were removed.
    """
    removed = 0
    for s in server.store.snapshot():
        pid = s.get("pid")
        if not isinstance(pid, int) or _pid_alive(pid):
            continue
        agent, session_id = s["agent"], s["vendor_session_id"]
        if server.store.delete(agent, session_id):
            server.transcript_mtimes.pop((agent, session_id), None)
            server.broadcaster.publish_remove(s)
            removed += 1
    return removed


def _mtime_ms(path: str) -> int | None:
    try:
        return int(Path(path).stat().st_mtime * 1000)
    except OSError:
        return None


def _refresh_titles(server: SessionServer) -> int:
    """Derive titles from the transcripts the hooks told us about.

    Scoped and gated for efficiency: only sessions whose transcript was
    modified *after the daemon started* (older ones are pre-daemon history we
    don't track), and only when the transcript's mtime has advanced since we
    last derived from it (tracked in `server.transcript_mtimes`). Re-deriving
    on change — not just once — means a later `/rename` propagates. Returns how
    many rows were (re)titled.
    """
    changed = 0
    for s in server.store.snapshot():
        tp = s.get("transcript_path")
        if not tp:
            continue
        mtime = _mtime_ms(tp)
        if mtime is None or mtime < server.started_ms:
            continue
        key = (s["agent"], s["vendor_session_id"])
        if server.transcript_mtimes.get(key) == mtime:
            continue
        server.transcript_mtimes[key] = mtime
        title = transcript.title_from_file(Path(tp))
        if title:
            # Claim ownership so the source-reader pass leaves this one alone.
            server.transcript_titled.add(key)
            updated = server.store.set_title(s["agent"], s["vendor_session_id"], title)
            if updated is not None:
                server.broadcaster.publish(updated)
                changed += 1
    return changed


def run_loop(server: SessionServer, interval_s: float, stop: threading.Event) -> None:
    """Run `poll_once` every `interval_s` until `stop` is set."""
    while not stop.is_set():
        try:
            poll_once(server)
        except Exception as e:
            print(f"pm agent serve: fallback poll failed: {e}")
        stop.wait(interval_s)
