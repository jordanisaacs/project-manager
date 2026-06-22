"""Stdlib threaded HTTP + SSE server for `pm agent serve`.

No third-party dependency: a `ThreadingHTTPServer` handles each request on
its own thread, SSE clients hold their handler thread fed by a per-client
queue, and `Broadcaster.publish` fans a changed session out to every
client whose subscription filter matches. Endpoints:

    POST /api/status   — ingest one hook event, upsert, broadcast
    GET  /api/stream   — SSE: filtered snapshot then per-session deltas
    GET  /api/health   — liveness: bound port, uptime, session count

Pure observer: there are no action endpoints.
"""

import contextlib
import json
import queue
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from project_manager.agent.serve import status
from project_manager.agent.serve import store as store_mod
from project_manager.agent.serve.store import Session, Store
from project_manager.paths import Paths

# How long an idle SSE connection waits before emitting a keepalive
# comment — also the cadence at which a dead client is noticed.
_SSE_KEEPALIVE_S = 15.0
_FILTER_PREFIXES = ("meta.",)
_FILTER_COLS = ("agent", "project", "status")


class Broadcaster:
    """Fan-out of SSE event payloads to subscribed clients."""

    def __init__(self) -> None:
        self._clients: dict[int, tuple[queue.Queue[dict[str, Any]], dict[str, str]]] = {}
        self._lock = threading.Lock()
        self._next_id = 0

    def register(self, filters: dict[str, str]) -> tuple[int, queue.Queue[dict[str, Any]]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)
        with self._lock:
            client_id = self._next_id
            self._next_id += 1
            self._clients[client_id] = (q, filters)
        return client_id, q

    def unregister(self, client_id: int) -> None:
        with self._lock:
            self._clients.pop(client_id, None)

    def _emit(self, session: Session, payload: dict[str, Any]) -> None:
        """Send PAYLOAD to every client whose filter matches SESSION."""
        with self._lock:
            targets = [
                q
                for q, filters in self._clients.values()
                if store_mod.session_matches(session, filters)
            ]
        for q in targets:
            # Drop for a wedged (full-queue) client rather than blocking the
            # writer; it re-syncs from the next snapshot on reconnect.
            with contextlib.suppress(queue.Full):
                q.put_nowait(payload)

    def publish(self, session: Session) -> None:
        self._emit(session, {"type": "update", "session": session})

    def publish_remove(self, session: Session) -> None:
        # Route by the removed session's own fields so it reaches exactly the
        # clients that were showing it.
        self._emit(
            session,
            {
                "type": "remove",
                "agent": session["agent"],
                "vendor_session_id": session["vendor_session_id"],
            },
        )

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


class SessionServer:
    """Owns the store + broadcaster and runs the HTTP server."""

    def __init__(self, store: Store, paths: Paths, port: int) -> None:
        self.store = store
        self.paths = paths
        self.port = port
        self.broadcaster = Broadcaster()
        self._started = time.monotonic()
        # Wall-clock start (ms) — only transcripts modified after this are
        # worth titling (older ones are pre-daemon history we don't track).
        self.started_ms = int(time.time() * 1000)
        # (agent, session_id) -> last transcript mtime (ms) we derived a title
        # from, so we re-derive only when the transcript actually changes.
        self.transcript_mtimes: dict[tuple[str, str], int] = {}
        # Sessions whose title is owned by the transcript pass (a parseable
        # transcript yielded a title). The source-reader title pass skips these
        # so the two don't fight; agents whose transcript we can't parse (e.g.
        # Cursor) stay out of this set and are titled from their source reader.
        self.transcript_titled: set[tuple[str, str]] = set()

    def ingest(self, event: dict[str, Any]) -> Session | None:
        """Apply a hook event and broadcast it if it changed a session."""
        if status.is_terminal_event(event.get("hook_event_name")):
            self._remove_for_event(event)
            return None
        session = self.store.ingest(event, self.paths)
        if session is not None:
            self.broadcaster.publish(session)
        return session

    def _remove_for_event(self, event: dict[str, Any]) -> None:
        """Drop the session named by a terminal (SessionEnd) event."""
        agent = str(event.get("agent", ""))
        session_id = str(event.get("session_id", ""))
        prev = self.store.get(agent, session_id)
        if prev is not None and self.store.delete(agent, session_id):
            self.transcript_mtimes.pop((agent, session_id), None)
            self.broadcaster.publish_remove(prev)

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "port": self.port,
            "uptime_s": round(time.monotonic() - self._started, 1),
            "sessions": len(self.store.snapshot()),
            "clients": self.broadcaster.client_count(),
        }

    def make_server(self) -> "_HTTPServer":
        """Bind the HTTP server and return it (does not start serving).

        Binding with `port=0` lets the OS pick a free port; `self.port` is
        updated to the resolved value so `/api/health` and tests report the
        real port.
        """
        httpd = _HTTPServer(("127.0.0.1", self.port), _Handler)
        httpd.app = self
        self.port = httpd.server_address[1]
        return httpd

    def serve_forever(self) -> None:
        self.make_server().serve_forever()


class _HTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    app: SessionServer


def _parse_filters(query: str) -> dict[str, str]:
    """Extract recognized filter params (`agent`/`project`/`status`/`meta.*`)."""
    out: dict[str, str] = {}
    for key, values in parse_qs(query).items():
        if not values:
            continue
        if key in _FILTER_COLS or key.startswith(_FILTER_PREFIXES):
            out[key] = values[0]
    return out


def _sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()


class _Handler(BaseHTTPRequestHandler):
    server: _HTTPServer  # set by ThreadingHTTPServer
    protocol_version = "HTTP/1.1"
    # Note: BaseHTTPRequestHandler logs each request to stderr — for a
    # systemd-managed daemon that lands in the journal, which is fine.

    @property
    def _app(self) -> SessionServer:
        return self.server.app

    # -- POST /api/status -----------------------------------------------------

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/status":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            event = json.loads(body) if body else {}
        except (ValueError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return
        if not isinstance(event, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "expected a json object"})
            return
        session = self._app.ingest(event)
        self._send_json(HTTPStatus.OK, {"ok": True, "accepted": session is not None})

    # -- GET /api/stream | /api/health ----------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json(HTTPStatus.OK, self._app.health())
            return
        if parsed.path == "/api/stream":
            self._stream(_parse_filters(parsed.query))
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _stream(self, filters: dict[str, str]) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        client_id, q = self._app.broadcaster.register(filters)
        try:
            snapshot = self._app.store.snapshot(filters)
            self.wfile.write(_sse({"type": "snapshot", "sessions": snapshot}))
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=_SSE_KEEPALIVE_S)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                else:
                    self.wfile.write(_sse(payload))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            # Client disconnected — normal SSE lifecycle, not an error.
            pass
        finally:
            self._app.broadcaster.unregister(client_id)

    # -- helpers --------------------------------------------------------------

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
