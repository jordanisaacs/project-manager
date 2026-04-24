"""`pm repo ls`."""
from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.repo import ls as ls_mod

from . import repo_app


@repo_app.command
def ls(
    *,
    offline: Annotated[bool, Parameter(negative="")] = False,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """List canonical repos with branch + upstream status.

    By default queries each repo's remote (`git ls-remote`) so the Remote
    column can tell you whether a fetch would bring new commits. Pass
    --offline to skip the network call and leave Remote blank.
    """
    paths = config.load()
    rows = ls_mod.ls(paths, check_remote=not offline)
    render.emit_rows(rows, ls_mod.COLUMNS, as_json=json)
    return 0
