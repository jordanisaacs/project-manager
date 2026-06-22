"""Claude Code source: one session per `.jsonl` under `~/.claude/projects/`.

Path encoding scheme (Claude-side, 1-way lossy): `/` → `-`, `.` → `-`.
`/home/alice/.projects/foo` encodes as `-home-alice--projects-foo`. We
only need the forward direction — given a pm project's owned paths, we
encode each one and look for a matching directory.

Per-session titles come from the shared `agent.transcript` library.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from project_manager.agent import transcript
from project_manager.agent.sources._entry import SessionEntry

_AGENT = "claude"


async def fetch(owned_paths: set[Path], limit: int) -> list[SessionEntry]:
    """Return up to `limit` most-recently-active Claude sessions for these cwds.

    Stats every `.jsonl` in the matched project dirs (cheap — metadata
    only), picks the top-N by mtime, then opens only those N files to
    derive titles. Runs the whole sync body in one `to_thread` so the
    caller's `asyncio.gather` fan-out across sources sees real parallelism.
    """
    return await asyncio.to_thread(_fetch_sync, owned_paths, limit)


def _fetch_sync(owned_paths: set[Path], limit: int) -> list[SessionEntry]:
    root = Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return []
    candidates = _collect_candidates(root, owned_paths)
    if not candidates:
        return []
    candidates.sort(key=lambda t: t[2], reverse=True)
    # Walk candidates in mtime order and keep taking valid sessions
    # until we have `limit` of them (or run out). Guarantees we return
    # the N most-recent *valid* sessions — a fixed `* 2` overfetch
    # could miss valid ones if empties cluster at the top.
    entries: list[SessionEntry] = []
    for cwd, f, mtime in candidates:
        entry = _read_session(cwd, f, mtime)
        if entry is not None:
            entries.append(entry)
        if len(entries) >= limit:
            break
    return entries


def _collect_candidates(
    root: Path,
    owned_paths: set[Path],
) -> list[tuple[Path, Path, float]]:
    out: list[tuple[Path, Path, float]] = []
    for cwd in owned_paths:
        project_dir = root / _encode_path(cwd)
        if not project_dir.is_dir():
            continue
        for f in project_dir.iterdir():
            if f.suffix != ".jsonl" or not f.is_file():
                continue
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            out.append((cwd, f, mtime))
    return out


def _encode_path(p: Path) -> str:
    return str(p).replace("/", "-").replace(".", "-")


def _read_session(cwd: Path, f: Path, mtime: float) -> SessionEntry | None:
    """Return a `SessionEntry`, or None if the transcript has no human content.

    A session with no derivable title (e.g. one that only ran slash-commands
    or `!bash` prompts) is skipped as a worthless resume target.
    """
    title = transcript.title_from_file(f)
    if title is None:
        return None
    return SessionEntry(
        agent=_AGENT,
        session_id=f.stem,
        title=title,
        last_active=datetime.fromtimestamp(mtime, tz=UTC),
        cwd=cwd,
    )
