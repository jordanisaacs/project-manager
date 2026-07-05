"""Tier-2 transcript classifier tests (port of tributary `jsonl_status.rs`)."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from project_manager.agent.serve import fallback
from project_manager.agent.serve import status as st
from project_manager.agent.serve.server import SessionServer
from project_manager.agent.serve.store import Store
from project_manager.agent.sources import SessionEntry
from project_manager.paths import Paths

# Above Linux's pid_max, so `os.kill` reliably reports it gone.
_DEAD_PID = 2**30


def _line(**obj: object) -> str:
    return json.dumps(obj)


@pytest.fixture
def server(tmp_path: Path) -> SessionServer:
    paths = Paths(
        repos=tmp_path / "repos",
        worktrees=tmp_path / "worktrees",
        projects=tmp_path / "projects",
        stacker_root=tmp_path / "stacker",
    )
    return SessionServer(Store(tmp_path / "serve.db", reset=True), paths, 0)


def _ingest(server: SessionServer, session_id: str, pid: int | None) -> None:
    event: dict[str, object] = {
        "agent": "claude",
        "session_id": session_id,
        "status": "running",
        "hook_event_name": "UserPromptSubmit",
    }
    if pid is not None:
        event["pid"] = pid
    server.store.ingest(event, server.paths)


def test_source_titles_skip_transcript_owned_sessions(server: SessionServer) -> None:
    # A session the transcript pass titled (tracked in `transcript_titled`) is
    # owned by `_refresh_titles`; the source-reader title must not touch it.
    server.store.ingest(
        {
            "agent": "claude",
            "session_id": "s1",
            "status": "running",
            "hook_event_name": "UserPromptSubmit",
        },
        server.paths,
    )
    server.store.set_title("claude", "s1", "Transcript Title")
    server.transcript_titled.add(("claude", "s1"))
    n = fallback._apply_source_reader_titles(server, {("claude", "s1"): "Other"})
    assert n == 0
    s = server.store.get("claude", "s1")
    assert s is not None
    assert s["title"] == "Transcript Title"


def test_source_titles_set_and_update_when_no_transcript(server: SessionServer) -> None:
    # Cursor-style: no transcript_path → source reader owns the title and
    # updates it when the vendor store is renamed.
    server.store.ingest(
        {
            "agent": "cursor",
            "session_id": "c1",
            "status": "running",
            "hook_event_name": "sessionStart",
        },
        server.paths,
    )
    assert fallback._apply_source_reader_titles(server, {("cursor", "c1"): "First"}) == 1
    s = server.store.get("cursor", "c1")
    assert s is not None
    assert s["title"] == "First"
    # A rename in the vendor store propagates...
    assert fallback._apply_source_reader_titles(server, {("cursor", "c1"): "Renamed"}) == 1
    s = server.store.get("cursor", "c1")
    assert s is not None
    assert s["title"] == "Renamed"
    # ...but an unchanged title is a no-op.
    assert fallback._apply_source_reader_titles(server, {("cursor", "c1"): "Renamed"}) == 0


def test_end_turn_is_idle() -> None:
    text = _line(type="assistant", message={"stop_reason": "end_turn"})
    assert fallback.classify_last_record(text) == st.IDLE


def test_stop_sequence_and_max_tokens_are_idle() -> None:
    assert (
        fallback.classify_last_record(
            _line(type="assistant", message={"stop_reason": "stop_sequence"})
        )
        == st.IDLE
    )
    assert (
        fallback.classify_last_record(
            _line(type="assistant", message={"stop_reason": "max_tokens"})
        )
        == st.IDLE
    )


def test_tool_use_tail_stays_working() -> None:
    # Ambiguous between "running" and "awaiting permission" from jsonl alone
    # — stay working and let the hook decide.
    text = _line(type="assistant", message={"stop_reason": "tool_use"})
    assert fallback.classify_last_record(text) == st.WORKING


def test_trailing_user_prompt_is_working() -> None:
    # A trailing user prompt is assumed to be a turn in flight (tributary's
    # `trailing_user_prompt_is_working`). Note: a cancelled turn fires no Claude
    # hook, so it stays "working" here until the hook row goes stale — see the
    # note in fallback.py (claude-code#9516).
    text = "\n".join(
        [
            _line(type="assistant", message={"stop_reason": "end_turn"}),
            _line(type="user", message={"content": "do the thing"}),
        ],
    )
    assert fallback.classify_last_record(text) == st.WORKING


def test_housekeeping_records_are_skipped() -> None:
    # A summary/system record after a finished assistant turn must not
    # change the verdict — only user/assistant records count.
    text = "\n".join(
        [
            _line(type="assistant", message={"stop_reason": "end_turn"}),
            _line(type="summary", summary="..."),
            _line(type="system", subtype="info"),
        ],
    )
    assert fallback.classify_last_record(text) == st.IDLE


def test_stop_reason_on_outer_record_is_honored() -> None:
    text = _line(type="assistant", stop_reason="end_turn")
    assert fallback.classify_last_record(text) == st.IDLE


def test_empty_or_unparseable_is_none() -> None:
    assert fallback.classify_last_record("") is None
    assert fallback.classify_last_record("not json\n") is None
    assert fallback.classify_last_record(_line(type="summary")) is None


# -- pid-liveness prune backstop ---------------------------------------------


def test_pid_alive_for_self() -> None:
    assert fallback._pid_alive(os.getpid())


def test_pid_not_alive_for_dead() -> None:
    assert not fallback._pid_alive(_DEAD_PID)


def test_pid_alive_for_nonpositive() -> None:
    # pid <= 0 has special signal semantics; never prune on them.
    assert fallback._pid_alive(0)
    assert fallback._pid_alive(-1)


def test_prune_dead_removes_only_dead_session(server: SessionServer) -> None:
    _ingest(server, "alive", os.getpid())
    _ingest(server, "dead", _DEAD_PID)
    assert fallback._prune_dead(server) == 1
    survivors = {s["vendor_session_id"] for s in server.store.snapshot()}
    assert survivors == {"alive"}


def test_prune_dead_leaves_pidless_session(server: SessionServer) -> None:
    # No recorded pid (e.g. a fallback-discovered Cursor session) → can't
    # check liveness, so it's never pruned.
    _ingest(server, "c1", None)
    assert fallback._prune_dead(server) == 0
    assert len(server.store.snapshot()) == 1


def test_poll_skips_sessions_replaced_by_clear(
    server: SessionServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = SessionEntry(
        agent="codex",
        session_id="old",
        title="stale",
        last_active=datetime.now(UTC),
        cwd=server.paths.projects / "alpha",
    )

    async def collect(
        _paths: Paths,
        _projects: list[str],
        _limit: int,
    ) -> list[tuple[str, SessionEntry]]:
        return [("alpha", entry)]

    monkeypatch.setattr(fallback.discovery, "list_project_dbs", lambda _paths: [("alpha", None)])
    monkeypatch.setattr(fallback, "_collect", collect)
    server.cleared_sessions.add(("codex", "old"))

    assert fallback.poll_once(server) == 0
    assert server.store.get("codex", "old") is None
