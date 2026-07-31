"""Generic transcript title extraction, shared across the agent integration.

Derives a human session title from a Claude Code (or Codex) JSONL
transcript. Used by the source readers (`pm agent ls`) and by the
`pm serve` daemon's fallback/title pass, so the rule lives in one
place — including the daemon titling a Codex session from the rollout file
its hook reports as `transcript_path`.

Title precedence (mirrors openui's):
    1. `custom-title` record — what `/rename` writes (an explicit rename).
    2. `ai-title` record      — Claude's own auto-generated title.
    3. `<synthetic>` "Summary:" — a compaction summary of the session.
    4. First genuinely-typed user prompt.

Codex rollout files carry none of the first three (no title/summary
records), so they resolve to (4): the first real user prompt, found in a
`response_item` → `message` (role `user`) record. Claude and Codex use
disjoint record-`type` sets, so a single scan handles both without sniffing.

`title_from_file` / `title_from_lines` return None when none of the above
are present (e.g. a session that only ran slash-commands), which callers
treat as "no human content".
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_SYNTHETIC = "<synthetic>"
_SUMMARY_PREFIX = "Summary:"

# Claude Code wraps injected system content — slash-command transcripts,
# reminder nags, command output — in XML-style tags on the `user` side of
# the conversation. Those are never what the user actually typed, so we skip
# them when hunting for the first-user-message fallback title.
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
    # Codex injects its session context as the first `user` turn — skip it the
    # same way, so the title is the first prompt the human actually typed.
    "<environment_context>",
    "<user_instructions>",
)


@dataclass
class TitleSignals:
    """The four title sources accumulated while scanning a transcript."""

    custom: str | None = None
    ai: str | None = None
    synthetic: str | None = None
    first_user: str | None = None


def flatten_content(content: object) -> str:
    """Join any `text` blocks; tolerant of str, list[dict], or odd shapes.

    Claude's user messages are typically bare strings but can be block lists
    (`[{"type":"text","text":"..."}, ...]`). Assistant messages are always
    block lists. We extract only `text` blocks and drop tool invocations — a
    tool-use block is not useful as a session title.
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
            # Claude uses `text` blocks; Codex user turns use `input_text`.
            if typed.get("type") in ("text", "input_text"):
                txt = typed.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
        return "\n".join(parts)
    return ""


def parse_record(raw: str) -> dict | None:
    """Parse one JSONL line into a dict, tolerating blank/garbage lines."""
    line = raw.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _str_field(obj: dict, key: str) -> str | None:
    val = obj.get(key)
    return val.strip() if isinstance(val, str) and val.strip() else None


def _is_typed_prompt(obj: dict) -> bool:
    """True for a genuine typed user prompt (not tool result / meta / sidechain)."""
    return (
        not obj.get("isMeta")
        and not obj.get("isSidechain")
        # When present, `promptSource` distinguishes a typed prompt from
        # synthetic user turns (tool results, command output, interrupts).
        and obj.get("promptSource", "typed") == "typed"
    )


def _synthetic_summary(obj: dict) -> str | None:
    """A compaction summary, if this record is one.
    `<synthetic>` also wraps non-response stubs and API-error envelopes, so
    only "Summary:"-prefixed text counts."""
    msg = obj.get("message")
    if isinstance(msg, dict) and msg.get("model") == _SYNTHETIC:
        text = flatten_content(msg.get("content")).lstrip()
        if text.startswith(_SUMMARY_PREFIX):
            return text
    return None


def scan_record(obj: dict, sig: TitleSignals) -> None:
    """Fold one transcript record into SIG by record type. Last write wins."""
    rtype = obj.get("type")
    if rtype == "custom-title":  # what /rename writes
        sig.custom = _str_field(obj, "customTitle") or sig.custom
    elif rtype == "ai-title":  # Claude's own auto title
        sig.ai = _str_field(obj, "aiTitle") or sig.ai
    elif rtype == "user":
        if sig.first_user is None and _is_typed_prompt(obj):
            candidate = flatten_content(obj.get("message", {}).get("content")).lstrip()
            if candidate and not candidate.startswith(_SYSTEM_USER_PREFIXES):
                sig.first_user = candidate
    elif rtype == "response_item":
        _scan_codex_response(obj, sig)
    else:
        sig.synthetic = _synthetic_summary(obj) or sig.synthetic


def _scan_codex_response(obj: dict, sig: TitleSignals) -> None:
    """Fold a Codex rollout `response_item` into SIG's first-user-prompt slot.

    Codex wraps each turn in `{"type":"response_item","payload":{"type":
    "message","role":..., "content":[{"type":"input_text",...}]}}`. Only the
    first real `user` message matters for a title; its session-context turn is
    skipped via `_SYSTEM_USER_PREFIXES` like Claude's injected turns.
    """
    if sig.first_user is not None:
        return
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return
    typed = cast("dict[str, object]", payload)
    if typed.get("type") != "message" or typed.get("role") != "user":
        return
    candidate = flatten_content(typed.get("content")).lstrip()
    if candidate and not candidate.startswith(_SYSTEM_USER_PREFIXES):
        sig.first_user = candidate


def pick_title(sig: TitleSignals) -> str | None:
    """Resolve accumulated SIG to a single title by precedence, or None."""
    # Explicit /rename, then Claude's auto ai-title — both are real titles.
    for cand in (sig.custom, sig.ai):
        if cand:
            collapsed = " ".join(cand.split())
            if collapsed:
                return collapsed
    # Compaction summary — Claude's condensed view of the whole session.
    if sig.synthetic:
        stripped = sig.synthetic.lstrip()
        if stripped.startswith(_SUMMARY_PREFIX):
            stripped = stripped[len(_SUMMARY_PREFIX) :].lstrip()
        collapsed = " ".join(stripped.split())
        if collapsed:
            return collapsed
    # Last resort: the first typed user prompt.
    if sig.first_user:
        collapsed = " ".join(sig.first_user.split())
        if collapsed:
            return collapsed
    return None


def title_from_lines(lines: Iterable[str]) -> str | None:
    """Derive a title from an iterable of raw JSONL line strings, or None."""
    sig = TitleSignals()
    for raw in lines:
        obj = parse_record(raw)
        if obj is not None:
            scan_record(obj, sig)
    return pick_title(sig)


def title_from_file(path: Path, *, tail_bytes: int | None = None) -> str | None:
    """Derive a title from the transcript at PATH, or None.

    With TAIL_BYTES, only the last that many bytes are read — cheaper for
    large transcripts, but the first-typed-prompt fallback (which lives at
    the head) won't be seen. Pass None to scan the whole file.
    """
    try:
        if tail_bytes:
            with path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - tail_bytes))
                text = fh.read().decode("utf-8", errors="replace")
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return title_from_lines(text.splitlines())
