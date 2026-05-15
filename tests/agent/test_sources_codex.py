import asyncio
import sqlite3
from pathlib import Path

import pytest

from project_manager.agent.sources import codex


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _make_db(home: Path) -> Path:
    db_path = home / ".codex" / "state_5.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    # Minimal subset of Codex's `threads` schema — only the columns we
    # SELECT need to exist. NOT NULL + DEFAULT columns are set so
    # inserts stay short.
    conn.execute("""
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            cwd TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            first_user_message TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    return db_path


def _insert(db: Path, row: dict) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO threads(id,cwd,title,first_user_message,updated_at,archived)"
        " VALUES(:id,:cwd,:title,:first_user_message,:updated_at,:archived)",
        {
            "title": "",
            "first_user_message": "",
            "archived": 0,
            "updated_at": 0,
            **row,
        },
    )
    conn.commit()
    conn.close()


def test_returns_empty_when_db_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _home(tmp_path, monkeypatch)
    assert asyncio.run(codex.fetch({Path("/any")}, 5)) == []


def test_filters_by_cwd_and_orders_by_updated_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path, monkeypatch)
    db = _make_db(home)
    _insert(db, {"id": "a", "cwd": "/proj/a", "title": "A", "updated_at": 100})
    _insert(db, {"id": "b", "cwd": "/proj/a", "title": "B", "updated_at": 300})
    _insert(db, {"id": "c", "cwd": "/proj/b", "title": "C", "updated_at": 500})
    _insert(db, {"id": "d", "cwd": "/proj/a", "first_user_message": "prompted", "updated_at": 200})
    entries = asyncio.run(codex.fetch({Path("/proj/a")}, 5))
    assert [e.session_id for e in entries] == ["b", "d", "a"]
    assert [e.title for e in entries] == ["B", "prompted", "A"]


def test_skips_archived_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path, monkeypatch)
    db = _make_db(home)
    _insert(db, {"id": "live", "cwd": "/p", "title": "L", "updated_at": 100})
    _insert(db, {"id": "dead", "cwd": "/p", "title": "D", "updated_at": 200, "archived": 1})
    entries = asyncio.run(codex.fetch({Path("/p")}, 5))
    assert [e.session_id for e in entries] == ["live"]


def test_skips_rows_with_no_title_and_no_first_user_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path, monkeypatch)
    db = _make_db(home)
    _insert(db, {"id": "empty", "cwd": "/p", "updated_at": 100})
    _insert(db, {"id": "real", "cwd": "/p", "title": "real title", "updated_at": 50})
    entries = asyncio.run(codex.fetch({Path("/p")}, 5))
    assert [e.session_id for e in entries] == ["real"]


def test_respects_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = _home(tmp_path, monkeypatch)
    db = _make_db(home)
    for i in range(10):
        _insert(db, {"id": f"s{i}", "cwd": "/p", "title": f"t{i}", "updated_at": i})
    entries = asyncio.run(codex.fetch({Path("/p")}, 3))
    assert [e.session_id for e in entries] == ["s9", "s8", "s7"]
