from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar, cast

import pytest

from project_manager.config import Omnigent
from project_manager.omnigent.client import ProjectsClient, _token_from_output


class _Handler(BaseHTTPRequestHandler):
    path_seen = ""
    authorization_seen: str | None = None
    writes_seen: ClassVar[list[tuple[str, str, object]]] = []

    def do_GET(self) -> None:
        type(self).path_seen = self.path
        type(self).authorization_seen = self.headers.get("Authorization")
        self._respond(
            {
                "object": "list",
                "data": [{"id": "p1", "name": "Demo", "config": {"agent_id": "a"}}],
            }
        )

    def do_POST(self) -> None:
        payload = self._payload()
        type(self).writes_seen.append(("POST", self.path, payload))
        assert isinstance(payload, dict)
        self._respond({"id": "created", **payload})

    def do_PATCH(self) -> None:
        payload = self._payload()
        type(self).writes_seen.append(("PATCH", self.path, payload))
        assert isinstance(payload, dict)
        data = cast("dict[str, Any]", payload)
        self._respond(
            {"id": "p1", "name": data.get("name", "Demo"), "config": data.get("config", {})}
        )

    def do_DELETE(self) -> None:
        type(self).writes_seen.append(("DELETE", self.path, None))
        self._respond({"id": "p1", "object": "project.deleted", "deleted": True})

    def _payload(self) -> object:
        length = int(self.headers["Content-Length"])
        return json.loads(self.rfile.read(length))

    def _respond(self, value: object) -> None:
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def api_server() -> Iterator[str]:
    _Handler.writes_seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield f"http://127.0.0.1:{port}/mount"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_client_uses_mount_path_and_bearer_environment(
    api_server: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential_source = "PM_OMNIGENT_TOKEN"
    monkeypatch.setenv(credential_source, "secret-token")
    client = ProjectsClient(
        Omnigent(
            server_url=api_server,
            host_id="host_1",
            token_env=credential_source,
        )
    )

    assert client.list()[0].config == {"agent_id": "a"}
    assert _Handler.path_seen == "/mount/v1/projects"
    assert _Handler.authorization_seen == "Bearer secret-token"


def test_client_sends_real_projects_crud_shapes(api_server: str) -> None:
    client = ProjectsClient(Omnigent(server_url=api_server, host_id="host_1"))

    created = client.create("Demo", {"host_id": "host_1", "workspace": "/projects/Demo"})
    updated = client.update(
        "p/1",
        name="Renamed",
        config={"host_id": "host_1", "workspace": "/projects/Renamed"},
    )
    client.delete("p/1")

    assert created.id == "created"
    assert updated.name == "Renamed"
    assert _Handler.writes_seen == [
        (
            "POST",
            "/mount/v1/projects",
            {
                "name": "Demo",
                "config": {"host_id": "host_1", "workspace": "/projects/Demo"},
            },
        ),
        (
            "PATCH",
            "/mount/v1/projects/p%2F1",
            {
                "name": "Renamed",
                "config": {"host_id": "host_1", "workspace": "/projects/Renamed"},
            },
        ),
        ("DELETE", "/mount/v1/projects/p%2F1", None),
    ]


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("plain-token\n", "plain-token"),
        ('{"access_token":"oauth-token","token_type":"Bearer"}', "oauth-token"),
        ('{"token":"session-token"}', "session-token"),
        ("{}", None),
    ],
)
def test_token_command_output_formats(output: str, expected: str | None) -> None:
    assert _token_from_output(output) == expected
