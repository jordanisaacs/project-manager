"""`pm serve` — run the project-manager daemon in the foreground."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import root
from project_manager.server import ProjectServer


@root.command
def serve(
    *,
    port: Annotated[int | None, Parameter(help="override [serve].port")] = None,
) -> int:
    """Run the project integration daemon in the foreground.

    The loopback server exposes project discovery, durable leases, and health.
    Lifecycle is normally owned by systemd (see pm-serve.service).
    """
    paths = config.load()
    cfg = config.serve()
    bind_port = port if port is not None else cfg.port

    server = ProjectServer(paths, bind_port, allowed_origins=cfg.allowed_origins)

    print(f"pm serve: listening on 127.0.0.1:{bind_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("pm serve: shutting down")
    return 0
