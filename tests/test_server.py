"""HTTP tests for the project-only ``pm serve`` daemon."""

import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from project_manager.paths import Paths
from project_manager.project import add as add_mod
from project_manager.server import ProjectServer, _HTTPServer

_PROTOCOL = {"X-PM-Protocol-Version": "1"}


@pytest.fixture
def server(tmp_path: Path) -> Iterator[ProjectServer]:
    paths = Paths(
        repos=tmp_path / "repos",
        worktrees=tmp_path / "worktrees",
        projects=tmp_path / "projects",
        stacker_root=tmp_path / "stacker",
    )
    app = ProjectServer(paths, 0)
    httpd: _HTTPServer = app.make_server()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield app
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def _request(
    app: ProjectServer,
    method: str,
    path: str,
    *,
    payload: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, http.client.HTTPMessage, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=2)
    body = json.dumps(payload) if payload is not None else None
    request_headers = {**({"Content-Type": "application/json"} if body else {}), **(headers or {})}
    try:
        conn.request(method, path, body=body, headers=request_headers)
        response = conn.getresponse()
        raw = response.read()
        return response.status, response.headers, json.loads(raw) if raw else {}
    finally:
        conn.close()


def _post(
    app: ProjectServer,
    path: str,
    payload: dict[str, object],
) -> tuple[int, dict[str, Any]]:
    status, _headers, body = _request(
        app,
        "POST",
        path,
        payload=payload,
        headers=_PROTOCOL,
    )
    return status, body


def test_health_has_project_only_schema(server: ProjectServer) -> None:
    status, _headers, body = _request(server, "GET", "/api/health")

    assert status == 200
    assert set(body) == {"ok", "port", "uptime_s", "project_protocol_version"}
    assert body["ok"] is True
    assert body["port"] == server.port
    assert body["project_protocol_version"] == 1


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/api/status"), ("GET", "/api/stream")],
)
def test_agent_tracking_endpoints_are_gone(
    server: ProjectServer,
    method: str,
    path: str,
) -> None:
    status, _headers, body = _request(
        server,
        method,
        path,
        payload={} if method == "POST" else None,
    )

    assert status == 404
    assert body == {"error": "not found"}


def test_project_api_requires_protocol_header(server: ProjectServer) -> None:
    status, _headers, body = _request(server, "GET", "/api/projects")

    assert status == 426
    assert body["protocol_version"] == 1


def test_project_api_lists_projects(server: ProjectServer) -> None:
    add_mod.add(server.paths, "demo", [])

    status, _headers, body = _request(server, "GET", "/api/projects", headers=_PROTOCOL)

    assert status == 200
    assert body["protocol_version"] == 1
    assert [project["name"] for project in body["data"]] == ["demo"]


def test_project_lease_http_lifecycle(server: ProjectServer) -> None:
    add_mod.add(server.paths, "demo", [])
    namespace = "https://omnigent.example"

    status, pending = _post(
        server,
        "/api/leases/acquire",
        {"project": "demo", "namespace": namespace, "pending_id": "pending-1"},
    )
    assert status == 201
    assert pending["state"] == "pending"

    status, finalized = _post(
        server,
        "/api/leases/finalize",
        {
            "project": "demo",
            "namespace": namespace,
            "pending_id": "pending-1",
            "session_id": "session-1",
        },
    )
    assert status == 200
    assert finalized["state"] == "finalized"

    status, _headers, leases = _request(
        server,
        "GET",
        f"/api/leases?{urlencode({'namespace': namespace})}",
        headers=_PROTOCOL,
    )
    assert status == 200
    assert [(row["project"], row["lease_id"]) for row in leases["data"]] == [
        ("demo", "session-1"),
    ]

    status, released = _post(
        server,
        "/api/leases/release",
        {"namespace": namespace, "session_id": "session-1"},
    )
    assert status == 200
    assert released == {"released": True, "projects": ["demo"]}


def test_project_api_cors_allows_loopback_and_configured_origins(
    server: ProjectServer,
) -> None:
    server.allowed_origins = frozenset({"https://omnigent.example"})
    for origin in ("http://localhost:5173", "https://omnigent.example"):
        status, headers, _body = _request(
            server,
            "GET",
            "/api/projects",
            headers={**_PROTOCOL, "Origin": origin},
        )
        assert status == 200
        assert headers.get("Access-Control-Allow-Origin") == origin


def test_project_api_cors_rejects_unconfigured_origin(server: ProjectServer) -> None:
    status, _headers, _body = _request(
        server,
        "GET",
        "/api/projects",
        headers={**_PROTOCOL, "Origin": "https://evil.example"},
    )

    assert status == 403


def test_project_api_cors_preflight(server: ProjectServer) -> None:
    status, headers, body = _request(
        server,
        "OPTIONS",
        "/api/leases/acquire",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, x-pm-protocol-version",
        },
    )

    assert status == 204
    assert body == {}
    assert headers.get("Access-Control-Allow-Origin") == "http://localhost:5173"
    assert headers.get("Access-Control-Allow-Methods") == "GET, POST, OPTIONS"
