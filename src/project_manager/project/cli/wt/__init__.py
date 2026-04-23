"""`pm project wt` sub-sub-app."""
from cyclopts import App

from project_manager.project.cli import project_app

wt_app = project_app.command(
    App(name="wt", help="manage worktrees within a project"),
)

from . import attach as _attach  # noqa: F401,E402
from . import create as _create  # noqa: F401,E402
from . import delete as _delete  # noqa: F401,E402
from . import detach as _detach  # noqa: F401,E402
