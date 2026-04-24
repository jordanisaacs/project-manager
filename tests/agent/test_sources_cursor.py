import asyncio
import json
import re
import sqlite3
from pathlib import Path

import pytest

from project_manager.agent.sources import cursor


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


_PATH_SEP_RE = re.compile(r"[/.]+")


def _encoded(cwd: Path) -> str:
    return _PATH_SEP_RE.sub("-", str(cwd)).lstrip("-")


def _mk_transcript(home: Path, cwd: Path, session_id: str, lines: list[dict]) -> Path:
    session_dir = (
        home / ".cursor" / "projects" / _encoded(cwd) / "agent-transcripts" / session_id
    )
    session_dir.mkdir(parents=True, exist_ok=True)
    f = session_dir / f"{session_id}.jsonl"
    f.write_text("".join(json.dumps(obj) + "\n" for obj in lines))
    return f


def _mk_store(home: Path, workspace: str, session_id: str, meta: dict) -> None:
    db = home / ".config" / "cursor" / "chats" / workspace / session_id / "store.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    hex_blob = json.dumps(meta).encode("utf-8").hex()
    conn.execute("INSERT INTO meta(key, value) VALUES('0', ?)", (hex_blob,))
    conn.commit()
    conn.close()


def test_encoding_collapses_dot_runs() -> None:
    """Regression: `/home/a.b/.projects/x` encodes to a single `-` between
    segments, not `--`. Cursor collapses consecutive path separators
    (both `/` and `.`) into one dash."""
    from project_manager.agent.sources.cursor import _encode_path
    assert (
        _encode_path(Path("/home/jordan.isaacs/.projects/pm-claude"))
        == "home-jordan-isaacs-projects-pm-claude"
    )


def test_returns_empty_when_cursor_root_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _home(tmp_path, monkeypatch)
    assert asyncio.run(cursor.fetch({Path("/x")}, 5)) == []


def test_prefers_store_db_name_over_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path, monkeypatch)
    cwd = Path("/proj/a")
    sid = "11111111-1111-1111-1111-111111111111"
    _mk_transcript(home, cwd, sid, [
        {"role": "user", "message": {"content": [
            {"type": "text", "text": "<user_query>\nsomething\n</user_query>"}]}},
    ])
    _mk_store(home, "wsabc", sid, {
        "agentId": sid, "name": "Refactor auth layer", "createdAt": 1700000000000,
    })
    entries = asyncio.run(cursor.fetch({cwd}, 5))
    assert entries[0].session_id == sid
    assert entries[0].title == "Refactor auth layer"
    assert entries[0].agent == "cursor"


def test_falls_back_to_first_user_query_when_no_store_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path, monkeypatch)
    cwd = Path("/proj/a")
    sid = "22222222-2222-2222-2222-222222222222"
    _mk_transcript(home, cwd, sid, [
        {"role": "user", "message": {"content": [
            {"type": "text", "text": "<user_query>\nhow do I do X?\n</user_query>"}]}},
    ])
    entries = asyncio.run(cursor.fetch({cwd}, 5))
    assert entries[0].title == "how do I do X?"


def test_skips_sessions_with_no_store_name_and_no_user_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path, monkeypatch)
    cwd = Path("/proj/a")
    sid = "33333333-3333-3333-3333-333333333333"
    # Transcript exists but contains no `<user_query>` — cursor's
    # equivalent of a bash-only / metadata-only session.
    _mk_transcript(home, cwd, sid, [
        {"role": "assistant", "message": {"content": [
            {"type": "text", "text": "automated turn"}]}},
    ])
    assert asyncio.run(cursor.fetch({cwd}, 5)) == []


def test_limits_across_sessions_under_one_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path, monkeypatch)
    cwd = Path("/proj/a")
    import os
    for i in range(4):
        sid = f"0000000{i}-0000-0000-0000-000000000000"
        f = _mk_transcript(home, cwd, sid, [
            {"role": "user", "message": {"content": [
                {"type": "text", "text": f"<user_query>\nq{i}\n</user_query>"}]}},
        ])
        os.utime(f, (1000 + i, 1000 + i))
    entries = asyncio.run(cursor.fetch({cwd}, 2))
    assert [e.title for e in entries] == ["q3", "q2"]
