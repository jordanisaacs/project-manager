"""`pm repo ls`."""
from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.repo import ls as ls_mod

from . import repo_app


@repo_app.command
def ls(
    *,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """List canonical repos with branch + upstream status."""
    paths = config.load()
    render.emit_rows(ls_mod.ls(paths), ls_mod.COLUMNS, as_json=json)
    return 0
