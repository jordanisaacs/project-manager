"""`pm project` sub-app."""

from cyclopts import App

from project_manager.cli._shared import root

project_app = root.command(
    App(name="project", help="manage projects"),
)

from . import create as _create  # noqa: F401,E402
from . import delete as _delete  # noqa: F401,E402
from . import lease as _lease  # noqa: F401,E402
from . import ls as _ls  # noqa: F401,E402
from . import status as _status  # noqa: F401,E402
from . import wt as _wt  # noqa: F401,E402
