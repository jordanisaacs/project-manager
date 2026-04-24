import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest

import project_manager.cli  # noqa: F401  — ensures agent sub-app is registered
from project_manager.agent import ls as ls_mod
from project_manager.agent.sources import REGISTRY, SessionEntry, parse_agents
from project_manager.paths import Paths
from project_manager.project import create as create_mod
from tests.helpers import git_pool


def _just(wts: list[str]) -> list[tuple[str, str]]:
    return [(w, w) for w in wts]


# --- parse_agents ---


def test_parse_agents_default_is_all_registered() -> None:
    assert parse_agents(None) == frozenset(REGISTRY)


def test_parse_agents_subset() -> None:
    assert parse_agents("claude,codex") == frozenset({"claude", "codex"})


def test_parse_agents_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown agent"):
        parse_agents("claud")


# --- orchestration ---


def _make_claude_session(
    home: Path, cwd: Path, session_id: str, prompt: str, mtime: float,
) -> None:
    import os
    encoded = str(cwd).replace("/", "-").replace(".", "-")
    session_dir = home / ".claude" / "projects" / encoded
    session_dir.mkdir(parents=True, exist_ok=True)
    f = session_dir / f"{session_id}.jsonl"
    f.write_text(
        json.dumps({"type": "user", "message": {"role": "user",
                                                "content": prompt}}) + "\n",
    )
    os.utime(f, (mtime, mtime))


def _make_codex_row(
    home: Path, *, thread_id: str, cwd: str, title: str, updated_at: int,
) -> None:
    db = home / ".codex" / "state_5.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS threads (
            id TEXT PRIMARY KEY, cwd TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            first_user_message TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "INSERT INTO threads(id,cwd,title,updated_at) VALUES(?,?,?,?)",
        (thread_id, cwd, title, updated_at),
    )
    conn.commit()
    conn.close()


def test_ls_merges_and_sorts_across_sources(
    pm_env: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just(["foo"]))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    project_dir = pm_env.project("demo")

    # Claude: older prompt, Codex: newer. Both must match the pm
    # project's owned paths via cwd = project_dir.
    _make_claude_session(
        fake_home, project_dir, "claude-1", "old claude prompt", mtime=1000,
    )
    _make_codex_row(
        fake_home, thread_id="codex-1", cwd=str(project_dir),
        title="new codex title", updated_at=2000,
    )

    rows = asyncio.run(
        ls_mod.ls(pm_env, ["demo"], limit=5, agents=parse_agents(None)),
    )
    assert [r.agent for r in rows] == ["codex", "claude"]
    assert rows[0].title == "new codex title"
    assert rows[1].title == "old claude prompt"


def test_ls_limit_applies_after_merge(
    pm_env: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just(["foo"]))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    project_dir = pm_env.project("demo")

    for i in range(3):
        _make_claude_session(
            fake_home, project_dir, f"c-{i}", f"cq{i}", mtime=100 + i,
        )
    for i in range(3):
        _make_codex_row(
            fake_home, thread_id=f"x-{i}", cwd=str(project_dir),
            title=f"xt{i}", updated_at=200 + i,
        )
    rows = asyncio.run(
        ls_mod.ls(pm_env, ["demo"], limit=2, agents=parse_agents(None)),
    )
    # Top 2 by last_active across all agents — the two newest codex rows.
    assert len(rows) == 2
    assert all(r.agent == "codex" for r in rows)


def test_ls_emits_placeholder_row_for_project_with_no_sessions(
    pm_env: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pm project with zero chats across all agents still gets a
    section — a single row where every data column is empty so the
    table renderer paints `-` placeholders. Lets the user distinguish
    "no sessions here" from "this project doesn't exist".
    """
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just(["foo"]))
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    rows = asyncio.run(ls_mod.ls(pm_env, ["demo"], 5, parse_agents(None)))
    assert len(rows) == 1
    placeholder = rows[0]
    assert placeholder.project == "demo"
    assert placeholder.agent == ""
    assert placeholder.session_id == ""
    assert placeholder.title == ""
    assert placeholder.last_active is None
    # The shared Column machinery substitutes `self.empty` ("-") for
    # any empty cell, so the rendered row is `demo | - | - | - | -`.
    for col in ls_mod.COLUMNS:
        assert col.render_cell(placeholder) == "-"


def test_ls_propagates_exceptions_from_sources(
    pm_env: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just(["foo"]))
    monkeypatch.setenv("HOME", str(tmp_path))

    async def boom(_owned: set[Path], _limit: int) -> list[SessionEntry]:
        raise RuntimeError("source fault")

    monkeypatch.setitem(REGISTRY, "claude", boom)
    # TaskGroup wraps the RuntimeError in an ExceptionGroup — we just
    # want proof the failure surfaces instead of being swallowed.
    with pytest.raises(BaseExceptionGroup) as exc_info:
        asyncio.run(ls_mod.ls(pm_env, ["demo"], 5, parse_agents(None)))
    assert any(
        isinstance(e, RuntimeError) and "source fault" in str(e)
        for e in exc_info.value.exceptions
    ) or any(
        isinstance(e, BaseExceptionGroup)
        for e in exc_info.value.exceptions
    )


def test_ls_runs_sources_concurrently(
    pm_env: Paths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each source sleeps for DELAY in a worker thread. With N sources
    fanned out concurrently, total wall time approaches DELAY, not
    N*DELAY. We assert strictly less than serial to prove parallelism
    without making the test flaky on a slow runner.
    """
    git_pool(pm_env, "foo", n=1)
    create_mod.create(pm_env, "demo", _just(["foo"]))
    monkeypatch.setenv("HOME", str(tmp_path))
    delay = 0.2

    async def slow(_owned: set[Path], _limit: int) -> list[SessionEntry]:
        await asyncio.to_thread(time.sleep, delay)
        return []

    for name in ("claude", "codex", "cursor"):
        monkeypatch.setitem(REGISTRY, name, slow)
    started = time.monotonic()
    asyncio.run(ls_mod.ls(pm_env, ["demo"], 5, parse_agents(None)))
    elapsed = time.monotonic() - started
    # Serial would be ~3*delay = 0.6s. Parallel should be ~delay = 0.2s.
    # Pick a threshold comfortably below 2*delay to avoid flakes.
    assert elapsed < 2 * delay
