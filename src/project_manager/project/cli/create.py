"""`pm project create`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.project import create as create_mod
from project_manager.project.spec import parse_wt_spec

from . import project_app


@project_app.command
def create(
    project: str,
    *,
    wt: str | None = None,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Create a project (optionally with initial worktrees).

    --wt takes a comma-separated spec: `<repo>` or `<name>:<repo>`.
    """
    paths = config.load()
    spec = parse_wt_spec(wt) if wt else []
    claimed = create_mod.create(paths, project, spec)
    render.emit_rows(claimed, create_mod.CREATED_COLUMNS, as_json=json)
    return 0
