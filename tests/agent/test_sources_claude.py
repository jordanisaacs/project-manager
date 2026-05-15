import asyncio
import json
import os
from pathlib import Path

import pytest

from project_manager.agent.sources import claude


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(obj) + "\n" for obj in lines))


def _encoded(cwd: Path) -> str:
    return str(cwd).replace("/", "-").replace(".", "-")


def test_returns_empty_when_claude_root_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _home(tmp_path, monkeypatch)
    cwd = Path("/some/where")
    assert asyncio.run(claude.fetch({cwd}, 5)) == []


def test_picks_first_user_message_when_no_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path, monkeypatch)
    cwd = Path("/proj/a")
    session_dir = home / ".claude" / "projects" / _encoded(cwd)
    f = session_dir / "abc123.jsonl"
    _write_jsonl(
        f,
        [
            {"type": "user", "message": {"role": "user", "content": "build me a thing"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "claude-4",
                    "content": [{"type": "text", "text": "ok"}],
                },
            },
        ],
    )
    entries = asyncio.run(claude.fetch({cwd}, 5))
    assert len(entries) == 1
    assert entries[0].session_id == "abc123"
    assert entries[0].title == "build me a thing"
    assert entries[0].agent == "claude"
    assert entries[0].cwd == cwd


def test_synthetic_summary_beats_first_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path, monkeypatch)
    cwd = Path("/proj/a")
    f = home / ".claude" / "projects" / _encoded(cwd) / "xyz.jsonl"
    synth_text = "Summary:\n\nFinal: did a thing."
    _write_jsonl(
        f,
        [
            {"type": "user", "message": {"role": "user", "content": "original prompt"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "<synthetic>",
                    "content": [{"type": "text", "text": synth_text}],
                },
            },
        ],
    )
    entries = asyncio.run(claude.fetch({cwd}, 5))
    assert entries[0].title == "Final: did a thing."


def test_skips_injected_system_user_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path, monkeypatch)
    cwd = Path("/proj/a")
    f = home / ".claude" / "projects" / _encoded(cwd) / "sysfirst.jsonl"
    caveat = "<local-command-caveat>skip me</local-command-caveat>"
    _write_jsonl(
        f,
        [
            {"type": "user", "message": {"role": "user", "content": caveat}},
            {"type": "user", "message": {"role": "user", "content": "real question here"}},
        ],
    )
    entries = asyncio.run(claude.fetch({cwd}, 5))
    assert entries[0].title == "real question here"


def test_ignores_non_summary_synthetic_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """<synthetic> model is used for non-response stubs + API error
    envelopes, not just compaction summaries. Only text starting with
    `Summary:` should count as a title; everything else falls back to
    the first real user message.
    """
    home = _home(tmp_path, monkeypatch)
    cwd = Path("/proj/a")
    f = home / ".claude" / "projects" / _encoded(cwd) / "xyz.jsonl"
    _write_jsonl(
        f,
        [
            {"type": "user", "message": {"role": "user", "content": "the actual question"}},
            # Not a summary — just a non-response stub Claude emits.
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "model": "<synthetic>",
                    "content": [{"type": "text", "text": "No response requested."}],
                },
            },
        ],
    )
    entries = asyncio.run(claude.fetch({cwd}, 5))
    assert entries[0].title == "the actual question"


def test_skips_bash_only_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path, monkeypatch)
    cwd = Path("/proj/a")
    # Every user message is a system-injected wrapper — no synthetic
    # summary either. Session is noise; shouldn't appear in the listing.
    f = home / ".claude" / "projects" / _encoded(cwd) / "bash-only.jsonl"
    _write_jsonl(
        f,
        [
            {"type": "user", "message": {"role": "user", "content": "<bash-input>ls</bash-input>"}},
            {
                "type": "user",
                "message": {"role": "user", "content": "<bash-stdout>hi</bash-stdout>"},
            },
        ],
    )
    assert asyncio.run(claude.fetch({cwd}, 5)) == []


def test_keeps_scanning_past_empties_to_fill_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if the N newest files are all bash-only, we should keep
    scanning older files until we have `limit` valid sessions. A fixed
    `* 2` overfetch could leave us short in this pattern.
    """
    home = _home(tmp_path, monkeypatch)
    cwd = Path("/proj/a")
    pdir = home / ".claude" / "projects" / _encoded(cwd)
    # 5 empty sessions with the newest mtimes, then 2 real sessions
    # with older mtimes. `-n 2` must still return both real ones.
    for i in range(5):
        f = pdir / f"empty-{i}.jsonl"
        _write_jsonl(
            f,
            [
                {
                    "type": "user",
                    "message": {"role": "user", "content": "<bash-input>x</bash-input>"},
                },
            ],
        )
        os.utime(f, (2000 + i, 2000 + i))
    for i in range(2):
        f = pdir / f"real-{i}.jsonl"
        _write_jsonl(
            f,
            [
                {"type": "user", "message": {"role": "user", "content": f"real prompt {i}"}},
            ],
        )
        os.utime(f, (1000 + i, 1000 + i))
    entries = asyncio.run(claude.fetch({cwd}, 2))
    assert [e.session_id for e in entries] == ["real-1", "real-0"]


def test_limit_applies_to_top_n_by_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path, monkeypatch)
    cwd = Path("/proj/a")
    pdir = home / ".claude" / "projects" / _encoded(cwd)
    for i in range(5):
        f = pdir / f"s{i}.jsonl"
        _write_jsonl(f, [{"type": "user", "message": {"role": "user", "content": f"q{i}"}}])
        # Monotonically increasing mtime; s4 is newest.
        os.utime(f, (1000 + i, 1000 + i))
    entries = asyncio.run(claude.fetch({cwd}, 2))
    assert [e.session_id for e in entries] == ["s4", "s3"]
