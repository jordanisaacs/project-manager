"""`pm stacker` sub-app and per-command module registration."""

from cyclopts import App

from project_manager.cli._shared import root

stacker_app = root.command(
    App(name="stacker", help="branch-stack tracking against pm's pool"),
)

from . import abort as _abort  # noqa: F401,E402
from . import absorb as _absorb  # noqa: F401,E402
from . import config as _config  # noqa: F401,E402
from . import continue_ as _continue  # noqa: F401,E402
from . import create as _create  # noqa: F401,E402
from . import guard as _guard  # noqa: F401,E402
from . import log as _log  # noqa: F401,E402
from . import ls as _ls  # noqa: F401,E402
from . import pr as _pr  # noqa: F401,E402
from . import push as _push  # noqa: F401,E402
from . import remove as _remove  # noqa: F401,E402
from . import rename as _rename  # noqa: F401,E402
from . import repair as _repair  # noqa: F401,E402
from . import reparent as _reparent  # noqa: F401,E402
from . import split as _split  # noqa: F401,E402
from . import sync as _sync  # noqa: F401,E402
