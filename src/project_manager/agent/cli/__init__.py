"""`pm agent` sub-app."""
from cyclopts import App

from project_manager.cli._shared import root

agent_app = root.command(
    App(name="agent", help="list recent coding-agent sessions"),
)

from . import ls as _ls  # noqa: F401,E402
