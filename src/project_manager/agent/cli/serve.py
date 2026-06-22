"""`pm agent serve` — run the session-tracking daemon (foreground)."""

import threading
from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.agent.serve import fallback
from project_manager.agent.serve.server import SessionServer
from project_manager.agent.serve.store import Store

from . import agent_app


@agent_app.command
def serve(
    *,
    port: Annotated[int | None, Parameter(help="override [serve].port")] = None,
) -> int:
    """Run the agent-session tracking daemon in the foreground.

    Binds a loopback HTTP server that ingests best-effort lifecycle-hook
    events (`POST /api/status`), keeps live state in an ephemeral WAL
    SQLite store, backfills via a transcript-tailing poller, and pushes
    updates over SSE (`GET /api/stream`). Lifecycle is meant to be owned
    by systemd (see integrations/systemd/pm-serve.service); this command
    just runs until interrupted.
    """
    paths = config.load()
    cfg = config.serve()
    bind_port = port if port is not None else cfg.port

    store = Store(cfg.db_path, reset=True)
    server = SessionServer(store, paths, bind_port)

    stop = threading.Event()
    poller = threading.Thread(
        target=fallback.run_loop,
        args=(server, cfg.fallback_interval, stop),
        name="pm-serve-fallback",
        daemon=True,
    )
    poller.start()

    print(f"pm agent serve: listening on 127.0.0.1:{bind_port} (db {cfg.db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("pm agent serve: shutting down")
    finally:
        stop.set()
    return 0
