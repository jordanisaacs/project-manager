"""Claude Code source: one session per `.jsonl` under `~/.claude/projects/`.

Path encoding scheme (Claude-side, 1-way lossy): `/` → `-`, `.` → `-`.
`/home/alice/.projects/foo` encodes as `-home-alice--projects-foo`. We
only need the forward direction — given a pm project's owned paths, we
encode each one and look for a matching directory.

Title fallback chain per session file:
    1. Last assistant message with `message.model == "<synthetic>"`
       (Claude's auto-summary).
    2. First `type == "user"` message's content.
    3. `<no summary>` literal.
"""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from project_manager.agent.sources._entry import SessionEntry

_AGENT = "claude"
_SYNTHETIC = "<synthetic>"
_SUMMARY_PREFIX = "Summary:"

# Claude Code wraps injected system content — slash-command transcripts,
# reminder nags, command output — in XML-style tags on the `user` side of
# the conversation. Those are never what the user actually typed, so we
# skip them when hunting for the "first user message" fallback title.
_SYSTEM_USER_PREFIXES = (
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<command-message>",
    "<command-name>",
    "<command-stdout>",
    "<command-stderr>",
    "<system-reminder>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
)


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
    """Return a `SessionEntry` or None if the session has no human content.

    "No human content" = no `<synthetic>` summary AND no user message that
    isn't a system-injected wrapper (`<bash-input>`, `<local-command-*>`,
    etc.). Sessions where the user only ran slash-commands or `!bash`
    prompts fall into that bucket and are worthless as resume targets.
    """
    first_user: str | None = None
    last_synthetic: str | None = None
    try:
        with f.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if first_user is None and obj.get("type") == "user":
                    candidate = _flatten_content(
                        obj.get("message", {}).get("content"),
                    ).lstrip()
                    if candidate and not candidate.startswith(
                        _SYSTEM_USER_PREFIXES,
                    ):
                        first_user = candidate
                msg = obj.get("message")
                if isinstance(msg, dict) and msg.get("model") == _SYNTHETIC:
                    text = _flatten_content(msg.get("content")).lstrip()
                    # `<synthetic>` is also used for non-response stubs
                    # ("No response requested.") and API error envelopes
                    # — only messages literally starting with "Summary:"
                    # are actual compaction summaries worth surfacing.
                    if text.startswith(_SUMMARY_PREFIX):
                        last_synthetic = text
    except OSError:
        return None
    title = _pick_title(last_synthetic, first_user)
    if title is None:
        return None
    return SessionEntry(
        agent=_AGENT,
        session_id=f.stem,
        title=title,
        last_active=datetime.fromtimestamp(mtime, tz=UTC),
        cwd=cwd,
    )


def _pick_title(synthetic: str | None, first_user: str | None) -> str | None:
    # Synthetic summary wins when present — it's Claude's condensed view
    # of the whole session, closer to a real title than the first prompt.
    if synthetic:
        stripped = synthetic.lstrip()
        if stripped.startswith(_SUMMARY_PREFIX):
            stripped = stripped[len(_SUMMARY_PREFIX) :].lstrip()
        collapsed = " ".join(stripped.split())
        if collapsed:
            return collapsed
    if first_user:
        collapsed = " ".join(first_user.split())
        if collapsed:
            return collapsed
    return None


def _flatten_content(content: object) -> str:
    """Join any `text` blocks; tolerant of str, list[dict], or unexpected shapes.

    Claude's user messages are typically bare strings but can be block
    lists (`[{"type":"text","text":"..."}, ...]`). Assistant messages
    are always block lists. We only extract `text` blocks and drop tool
    invocations — a tool-use block is not useful as a session title.
    """
    if content is None:
        return ""
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
