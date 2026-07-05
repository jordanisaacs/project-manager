"""Store tests.

Ported from db-agents `managedToAgentState.test.ts` (precedence) and
`managedSessionStore.test.ts` (late-bind id, refuse placeholder, lifecycle)
plus openui's snapshot/filter behavior.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from project_manager.agent.serve import status as st
from project_manager.agent.serve.store import FallbackReading, Store, is_placeholder_id
from project_manager.paths import Paths

_BASE_READING = FallbackReading(
    agent="claude",
    session_id="s1",
    cwd=None,
    project=None,
    status=st.IDLE,
    last_activity_at=2000,
)


def _reading(**over: object) -> FallbackReading:
    return replace(_BASE_READING, **over)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    return Paths(
        repos=tmp_path / "repos",
        worktrees=tmp_path / "worktrees",
        projects=tmp_path / "projects",
        stacker_root=tmp_path / "stacker",
    )


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(tmp_path / "serve.db", reset=True)


def _hook(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "agent": "claude",
        "session_id": "s1",
        "status": "pre_tool",
        "tool_name": "Edit",
        "hook_event_name": "PreToolUse",
    }
    base.update(over)
    return base


# -- placeholder ids (db-agents managedSessionStore.test.ts) ------------------


@pytest.mark.parametrize("bad", ["", "  ", "-", "unknown", "null", "none", "pending", None])
def test_is_placeholder_id(bad: str | None) -> None:
    assert is_placeholder_id(bad)


def test_ingest_refuses_placeholder_session_id(store: Store, paths: Paths) -> None:
    assert store.ingest(_hook(session_id=""), paths) is None
    assert store.ingest(_hook(session_id="unknown"), paths) is None
    assert store.snapshot() == []


# -- ingest + activity-gating -------------------------------------------------


def test_ingest_sets_status_activity_and_bumps(store: Store, paths: Paths) -> None:
    s = store.ingest(_hook(), paths, now_ms=1000)
    assert s is not None
    assert s["status"] == st.WORKING
    assert s["activity"] == "writing"
    assert s["last_activity_at"] == 1000


def test_session_start_does_not_clobber_last_activity(store: Store, paths: Paths) -> None:
    store.ingest(_hook(), paths, now_ms=1000)
    s = store.ingest(_hook(status="idle", hook_event_name="SessionStart"), paths, now_ms=9000)
    assert s is not None
    assert s["status"] == st.IDLE  # status still updates
    assert s["last_activity_at"] == 1000  # but the activity time does not


def test_late_session_id_upserts_metadata(store: Store, paths: Paths) -> None:
    # First event carries meta but minimal status; a later event refines it
    # on the same (agent, session_id) row rather than creating a second.
    store.ingest(
        _hook(status="idle", hook_event_name="SessionStart", meta={"SOURCE": "emacs"}), paths
    )
    store.ingest(_hook(status="running", hook_event_name="UserPromptSubmit"), paths)
    rows = store.snapshot()
    assert len(rows) == 1
    assert rows[0]["meta"] == {"SOURCE": "emacs"}
    assert rows[0]["status"] == st.WORKING


def test_cwd_resolves_to_project(store: Store, paths: Paths) -> None:
    cwd = paths.projects / "myproj" / "sub"
    s = store.ingest(_hook(cwd=str(cwd)), paths)
    assert s is not None
    assert s["project"] == "myproj"


# -- snapshot + filtering -----------------------------------------------------


def test_snapshot_filters(store: Store, paths: Paths) -> None:
    store.ingest(_hook(session_id="a", meta={"SOURCE": "emacs"}), paths)
    store.ingest(_hook(agent="codex", session_id="b"), paths)
    assert len(store.snapshot()) == 2
    assert len(store.snapshot({"agent": "codex"})) == 1
    assert len(store.snapshot({"meta.SOURCE": "emacs"})) == 1
    assert store.snapshot({"meta.SOURCE": "shell"}) == []


# -- fallback status precedence (db-agents managedToAgentState.test.ts) -------


def test_fallback_never_overrides_fresh_hook(store: Store, paths: Paths) -> None:
    store.ingest(_hook(status="running", hook_event_name="UserPromptSubmit"), paths, now_ms=1000)
    # A fresh hook row's status is untouched by the fallback (returns None).
    out = store.apply_fallback(_reading(status=st.IDLE), stale_ms=60_000, now_ms=1500)
    assert out is None
    s = store.get("claude", "s1")
    assert s is not None
    assert s["status"] == st.WORKING


def test_fallback_applies_when_no_hook(store: Store) -> None:
    out = store.apply_fallback(
        _reading(agent="cursor", session_id="c1", cwd="/x", status=st.UNKNOWN),
        stale_ms=60_000,
    )
    assert out is not None
    assert out["source"] == "fallback"


def test_fallback_applies_when_hook_is_stale(store: Store, paths: Paths) -> None:
    store.ingest(_hook(status="running", hook_event_name="UserPromptSubmit"), paths, now_ms=1000)
    out = store.apply_fallback(_reading(status=st.IDLE), stale_ms=500, now_ms=99_999)
    assert out is not None
    assert out["status"] == st.IDLE


def test_fallback_unknown_never_downgrades_known_status(store: Store, paths: Paths) -> None:
    # A hooked Cursor session goes idle; the fallback can't classify Cursor
    # (UNKNOWN), so even once the hook is stale it must not clobber idle→unknown.
    store.ingest(_hook(agent="cursor", status="idle", hook_event_name="stop"), paths, now_ms=1000)
    out = store.apply_fallback(
        _reading(agent="cursor", status=st.UNKNOWN), stale_ms=500, now_ms=99_999
    )
    assert out is None
    s = store.get("cursor", "s1")
    assert s is not None
    assert s["status"] == st.IDLE


# -- titles (set_title) + transcript_path -------------------------------------


def test_ingest_stores_transcript_path(store: Store, paths: Paths) -> None:
    s = store.ingest(_hook(transcript_path="/home/u/.claude/projects/x/s1.jsonl"), paths)
    assert s is not None
    assert s["transcript_path"] == "/home/u/.claude/projects/x/s1.jsonl"


def test_ingest_stores_pid(store: Store, paths: Paths) -> None:
    s = store.ingest(_hook(pid=4242), paths)
    assert s is not None
    assert s["pid"] == 4242


def test_delete(store: Store, paths: Paths) -> None:
    store.ingest(_hook(), paths)
    assert store.get("claude", "s1") is not None
    assert store.delete("claude", "s1") is True
    assert store.get("claude", "s1") is None
    # deleting an absent row reports no-op.
    assert store.delete("claude", "s1") is False


def test_set_title(store: Store, paths: Paths) -> None:
    store.ingest(_hook(), paths)
    s = store.get("claude", "s1")
    assert s is not None
    assert s["title"] is None
    out = store.set_title("claude", "s1", "Wire up SSE")
    assert out is not None
    assert out["title"] == "Wire up SSE"
    # status/source untouched; idempotent (no change → None).
    assert out["status"] == st.WORKING
    assert store.set_title("claude", "s1", "Wire up SSE") is None
    # unknown session → None.
    assert store.set_title("claude", "nope", "x") is None
