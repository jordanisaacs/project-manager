"""`pm stacker pr` — sub-app for inspecting/repairing the cached PR association."""
from cyclopts import App

from project_manager.stacker.commands import stacker_app

pr_app = stacker_app.command(
    App(
        name="pr",
        help="manage the cached PR association for tracked branches",
    ),
)

from . import refresh as _refresh  # noqa: F401,E402
from . import unlink as _unlink  # noqa: F401,E402
