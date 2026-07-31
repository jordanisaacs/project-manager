"""HTTP/SSE server tests (port of openui `api.sessions.test.ts` behavior).

Spins up the real `SessionServer` on an OS-picked port, drives it over
loopback HTTP, and asserts a POSTed status is reflected on the SSE stream
— including server-side filtering.
"""

import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from project_manager.agent.serve.server import SessionServer, _HTTPServer
from project_manager.agent.serve.store import Store
from project_manager.paths import Paths
from project_manager.project import add as add_mod


@pytest.fixture
def server(tmp_path: Path) -> Iterator[SessionServer]:
    paths = Paths(
        repos=tmp_path / "repos",
        worktrees=tmp_path / "worktrees",
        projects=tmp_path / "projects",
        stacker_root=tmp_path / "stacker",
    )
    app = SessionServer(Store(tmp_path / "serve.db", reset=True), paths, 0)
    httpd: _HTTPServer = app.make_server()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield app
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _conn(app: SessionServer) -> http.client.HTTPConnection:
    return http.client.HTTPConnection("127.0.0.1", app.port, timeout=2)


def _post(
    app: SessionServer,
    payload: dict[str, object],
    path: str = "/api/status",
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    conn = _conn(app)
    try:
        conn.request(
            "POST",
            path,
            body=json.dumps(payload),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        return json.loads(conn.getresponse().read())
    finally:
        conn.close()


def _snapshot(app: SessionServer, query: str = "") -> dict[str, Any]:
    """Connect to the SSE stream and return the first (snapshot) event."""
    conn = _conn(app)
    try:
        conn.request("GET", f"/api/stream{query}")
        resp = conn.getresponse()
        while True:
            raw = resp.readline()
            if not raw:
                break
            line = raw.decode().strip()
            if line.startswith("data:"):
                return json.loads(line[len("data:") :])
    finally:
        conn.close()
    msg = "no snapshot event received"
    raise AssertionError(msg)


def test_health(server: SessionServer) -> None:
    conn = _conn(server)
    try:
        conn.request("GET", "/api/health")
        body = json.loads(conn.getresponse().read())
    finally:
        conn.close()
    assert body["ok"] is True
    assert body["port"] == server.port


def test_post_reflected_on_stream(server: SessionServer) -> None:
    accepted = _post(
        server,
        {
            "agent": "claude",
            "session_id": "s1",
            "status": "pre_tool",
            "hook_event_name": "PreToolUse",
        },
    )
    assert accepted == {"ok": True, "accepted": True}

    snap = _snapshot(server)
    assert snap["type"] == "snapshot"
    sessions = snap["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["agent"] == "claude"
    assert sessions[0]["vendor_session_id"] == "s1"
    assert sessions[0]["status"] == "working"


def test_stream_filtered_by_meta(server: SessionServer) -> None:
    _post(
        server,
        {"agent": "claude", "session_id": "mine", "status": "running", "meta": {"SOURCE": "emacs"}},
    )
    _post(server, {"agent": "codex", "session_id": "other", "status": "running"})

    assert len(_snapshot(server)["sessions"]) == 2
    filtered = _snapshot(server, "?meta.SOURCE=emacs")["sessions"]
    assert len(filtered) == 1
    assert filtered[0]["vendor_session_id"] == "mine"


def test_session_end_removes_session(server: SessionServer) -> None:
    _post(
        server,
        {
            "agent": "claude",
            "session_id": "s1",
            "status": "running",
            "hook_event_name": "UserPromptSubmit",
        },
    )
    assert len(_snapshot(server)["sessions"]) == 1
    # A SessionEnd event is terminal: the row is dropped, not left as a ghost.
    accepted = _post(
        server,
        {"agent": "claude", "session_id": "s1", "hook_event_name": "SessionEnd"},
    )
    assert accepted == {"ok": True, "accepted": False}
    assert _snapshot(server)["sessions"] == []


def test_codex_clear_start_replaces_previous_session(server: SessionServer) -> None:
    meta = {"SOURCE": "emacs", "EMACS": "42", "BUF": "42-7"}
    _post(
        server,
        {
            "agent": "codex",
            "session_id": "old",
            "status": "running",
            "hook_event_name": "UserPromptSubmit",
            "meta": meta,
            "pid": 1234,
        },
    )
    accepted = _post(
        server,
        {
            "agent": "codex",
            "session_id": "new",
            "status": "idle",
            "hook_event_name": "SessionStart",
            "source": "clear",
            "meta": meta,
            "pid": 1234,
        },
    )

    assert accepted == {"ok": True, "accepted": True}
    sessions = _snapshot(server)["sessions"]
    assert [s["vendor_session_id"] for s in sessions] == ["new"]
    assert server.is_cleared("codex", "old")

    stale = _post(
        server,
        {"agent": "codex", "session_id": "old", "status": "idle", "hook_event_name": "Stop"},
    )
    assert stale == {"ok": True, "accepted": False}
    assert [s["vendor_session_id"] for s in _snapshot(server)["sessions"]] == ["new"]


def test_placeholder_session_id_rejected(server: SessionServer) -> None:
    accepted = _post(server, {"agent": "claude", "session_id": "", "status": "running"})
    assert accepted == {"ok": True, "accepted": False}
    assert _snapshot(server)["sessions"] == []


def test_project_api_requires_protocol_header(server: SessionServer) -> None:
    conn = _conn(server)
    try:
        conn.request("GET", "/api/projects")
        response = conn.getresponse()
        body = json.loads(response.read())
    finally:
        conn.close()

    assert response.status == 426
    assert body["protocol_version"] == 1


def test_project_lease_http_lifecycle(server: SessionServer) -> None:
    add_mod.add(server.paths, "demo", [])
    protocol = {"X-PM-Protocol-Version": "1"}
    namespace = "https://omnigent.example"

    pending = _post(
        server,
        {"project": "demo", "namespace": namespace, "pending_id": "pending-1"},
        "/api/leases/acquire",
        protocol,
    )
    assert pending["state"] == "pending"

    finalized = _post(
        server,
        {
            "project": "demo",
            "namespace": namespace,
            "pending_id": "pending-1",
            "session_id": "session-1",
        },
        "/api/leases/finalize",
        protocol,
    )
    assert finalized["state"] == "finalized"

    conn = _conn(server)
    try:
        conn.request(
            "GET",
            f"/api/leases?{urlencode({'namespace': namespace})}",
            headers=protocol,
        )
        leases = json.loads(conn.getresponse().read())
    finally:
        conn.close()
    assert [(row["project"], row["lease_id"]) for row in leases["data"]] == [("demo", "session-1")]

    released = _post(
        server,
        {"namespace": namespace, "session_id": "session-1"},
        "/api/leases/release",
        protocol,
    )
    assert released == {"released": True, "projects": ["demo"]}


def test_project_api_cors_allows_loopback_and_configured_origins(server: SessionServer) -> None:
    protocol = {"X-PM-Protocol-Version": "1"}
    for origin in ("http://localhost:5173", "https://omnigent.example"):
        server.allowed_origins = frozenset({"https://omnigent.example"})
        conn = _conn(server)
        try:
            conn.request("GET", "/api/projects", headers={**protocol, "Origin": origin})
            response = conn.getresponse()
            response.read()
        finally:
            conn.close()
        assert response.status == 200
        assert response.getheader("Access-Control-Allow-Origin") == origin


def test_project_api_cors_rejects_unconfigured_origin(server: SessionServer) -> None:
    conn = _conn(server)
    try:
        conn.request(
            "GET",
            "/api/projects",
            headers={"X-PM-Protocol-Version": "1", "Origin": "https://evil.example"},
        )
        response = conn.getresponse()
        response.read()
    finally:
        conn.close()

    assert response.status == 403
