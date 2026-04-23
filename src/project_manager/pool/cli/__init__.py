"""`pm pool` sub-app."""
from cyclopts import App

from project_manager.cli._shared import root

pool_app = root.command(
    App(name="pool", help="manage worktree pool"),
)

from . import add as _add  # noqa: F401,E402
from . import gc_ops as _gc_ops  # noqa: F401,E402
from . import ls as _ls  # noqa: F401,E402
