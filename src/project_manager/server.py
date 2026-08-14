"""Loopback HTTP server for project discovery and durable leases.

``pm serve`` intentionally has no agent-session state.  Live agent tracking
belongs to the Emacs/Ghostel integration; this server only exposes project
topology, leases, and liveness for browser integrations.
"""

import json
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse, urlsplit, urlunsplit

from project_manager import integration
from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.project import discovery
from project_manager.project import lease as lease_mod

_PROTOCOL_HEADER = "X-PM-Protocol-Version"
_ALLOWED_REQUEST_HEADERS = {"content-type", _PROTOCOL_HEADER.lower()}
_PROJECT_API_PATHS = {"/api/projects", "/api/leases"}
_LEASE_ACTION_PATHS = {
    "/api/leases/acquire",
    "/api/leases/finalize",
    "/api/leases/release",
}


class ProjectServer:
    """Serve project topology and leases over loopback HTTP."""

    def __init__(
        self,
        paths: Paths,
        port: int,
        *,
        allowed_origins: tuple[str, ...] = (),
    ) -> None:
        self.paths = paths
        self.port = port
        self.allowed_origins = frozenset(allowed_origins)
        self._started = time.monotonic()

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "port": self.port,
            "uptime_s": round(time.monotonic() - self._started, 1),
            "project_protocol_version": integration.PROTOCOL_VERSION,
        }

    def origin_allowed(self, origin: str) -> bool:
        """Return whether a browser origin may call this loopback daemon."""
        normalized = _normalize_origin(origin)
        if normalized is None:
            return False
        if normalized in self.allowed_origins:
            return True
        parsed = urlsplit(normalized)
        return parsed.hostname in {"localhost", "127.0.0.1", "::1"}

    def make_server(self) -> "_HTTPServer":
        """Bind and return the HTTP server without starting its loop."""
        httpd = _HTTPServer(("127.0.0.1", self.port), _Handler)
        httpd.app = self
        self.port = httpd.server_address[1]
        return httpd

    def serve_forever(self) -> None:
        self.make_server().serve_forever()


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    app: ProjectServer


class _Handler(BaseHTTPRequestHandler):
    server: _HTTPServer
    protocol_version = "HTTP/1.1"

    @property
    def _app(self) -> ProjectServer:
        return self.server.app

    def do_OPTIONS(self) -> None:
        parsed = urlparse(self.path)
        if not _is_project_api(parsed.path):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._require_allowed_origin():
            return
        requested_method = self.headers.get("Access-Control-Request-Method", "").upper()
        if requested_method not in {"GET", "POST"}:
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"})
            return
        requested_headers = {
            value.strip().lower()
            for value in self.headers.get("Access-Control-Request-Headers", "").split(",")
            if value.strip()
        }
        if not requested_headers.issubset(_ALLOWED_REQUEST_HEADERS):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "unsupported request headers"})
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            f"Content-Type, {_PROTOCOL_HEADER}",
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in _LEASE_ACTION_PATHS:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._require_allowed_origin() or not self._require_protocol():
            return
        self._post_lease(parsed.path)

    def _post_lease(self, path: str) -> None:
        payload = self._read_json_object()
        if payload is None:
            return
        try:
            namespace = str(payload.get("namespace", ""))
            if path == "/api/leases/acquire":
                acquired = lease_mod.acquire_pending(
                    self._app.paths,
                    str(payload.get("project", "")),
                    namespace,
                    str(payload.get("pending_id", "")),
                )
                self._send_json(HTTPStatus.CREATED, acquired.__pm_json__())
                return
            if path == "/api/leases/finalize":
                finalized = lease_mod.finalize(
                    self._app.paths,
                    str(payload.get("project", "")),
                    namespace,
                    str(payload.get("pending_id", "")),
                    str(payload.get("session_id", "")),
                )
                self._send_json(HTTPStatus.OK, finalized.__pm_json__())
                return
            releases = lease_mod.release_for_holder(
                self._app.paths,
                namespace,
                str(payload.get("session_id", payload.get("pending_id", ""))),
            )
            self._send_json(
                HTTPStatus.OK,
                {
                    "released": bool(releases),
                    "projects": [release.project for release in releases],
                },
            )
        except (ProjectError, ValueError) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            if self._require_allowed_origin():
                self._send_json(HTTPStatus.OK, self._app.health())
            return
        if not _is_project_api(parsed.path):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._require_allowed_origin() or not self._require_protocol():
            return
        self._get_project_api(parsed.path, parsed.query)

    def _get_project_api(self, path: str, query: str) -> None:
        if path == "/api/projects" or path.startswith("/api/projects/"):
            projects = integration.projects(self._app.paths)
            if path.startswith("/api/projects/"):
                name = unquote(path.removeprefix("/api/projects/"))
                project = next((row for row in projects if row.name == name), None)
                if project is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "project not found"})
                    return
                self._send_json(HTTPStatus.OK, project.__pm_json__())
                return
            self._send_json(
                HTTPStatus.OK,
                {
                    "object": "list",
                    "protocol_version": integration.PROTOCOL_VERSION,
                    "data": [project.__pm_json__() for project in projects],
                },
            )
            return

        namespace = parse_qs(query).get("namespace", [""])[0]
        try:
            project_names = [name for name, _db_path in discovery.list_project_dbs(self._app.paths)]
            leases = lease_mod.list_for_projects(
                self._app.paths,
                project_names,
                holder=namespace,
            )
        except ValueError as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "object": "list",
                "protocol_version": integration.PROTOCOL_VERSION,
                "data": [lease.__pm_json__() for lease in leases],
            },
        )

    def _read_json_object(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            payload = json.loads(body) if body else {}
        except (ValueError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return None
        if not isinstance(payload, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "expected a json object"})
            return None
        return payload

    def _require_protocol(self) -> bool:
        if self.headers.get(_PROTOCOL_HEADER) == str(integration.PROTOCOL_VERSION):
            return True
        self._send_json(
            HTTPStatus.UPGRADE_REQUIRED,
            {
                "error": "unsupported or missing project protocol version",
                "protocol_version": integration.PROTOCOL_VERSION,
            },
        )
        return False

    def _require_allowed_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None or self._app.origin_allowed(origin):
            return True
        self._send_json(HTTPStatus.FORBIDDEN, {"error": "origin not allowed"})
        return False

    def _send_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin is None or not self._app.origin_allowed(origin):
            return
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Vary", "Origin")

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)


def _normalize_origin(origin: str) -> str | None:
    parsed = urlsplit(origin.strip())
    try:
        _ = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), "", "", ""))


def _is_project_api(path: str) -> bool:
    return path in _PROJECT_API_PATHS or path.startswith(("/api/projects/", "/api/leases/"))
