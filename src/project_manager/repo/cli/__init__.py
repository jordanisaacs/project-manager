"""`pm repo` sub-app."""

from cyclopts import App

from project_manager.cli._shared import root

repo_app = root.command(
    App(name="repo", help="manage canonical repos under ~/.repos"),
)

from . import ls as _ls  # noqa: F401,E402
from . import maintenance as _maintenance  # noqa: F401,E402
from . import pull as _pull  # noqa: F401,E402
