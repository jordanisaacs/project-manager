"""`pm serve` — run the project-manager daemon in the foreground."""

import threading
from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.agent.serve import fallback
from project_manager.agent.serve.server import SessionServer
from project_manager.agent.serve.store import Store
from project_manager.cli._shared import root


@root.command
def serve(
    *,
    port: Annotated[int | None, Parameter(help="override [serve].port")] = None,
) -> int:
    """Run the project and agent-session daemon in the foreground.

    The loopback server exposes durable project leases as well as the existing
    hook ingestion, session status, health, and SSE endpoints. Lifecycle is
    normally owned by systemd (see integrations/systemd/pm-serve.service).
    """
    paths = config.load()
    cfg = config.serve()
    bind_port = port if port is not None else cfg.port

    store = Store(cfg.db_path, reset=True)
    server = SessionServer(store, paths, bind_port, allowed_origins=cfg.allowed_origins)

    stop = threading.Event()
    poller = threading.Thread(
        target=fallback.run_loop,
        args=(server, cfg.fallback_interval, stop),
        name="pm-serve-fallback",
        daemon=True,
    )
    poller.start()

    print(f"pm serve: listening on 127.0.0.1:{bind_port} (db {cfg.db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("pm serve: shutting down")
    finally:
        stop.set()
    return 0
