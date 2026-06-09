"""`pm project wt add`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.cli._shared import ProjectFlag
from project_manager.project import add as add_mod
from project_manager.project import current
from project_manager.project.spec import parse_wt_spec

from . import wt_app


@wt_app.command
def add(
    spec: str,
    flag: ProjectFlag = ProjectFlag(),
    *,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Add worktree(s) to an existing project.

    <spec> is the same format as `pm project create --wt`:
    comma-separated `<repo>` or `<name>:<repo>`.
    """
    paths = config.load()
    project = current.resolve_project(paths, flag.project)
    items = parse_wt_spec(spec)
    claimed = add_mod.add(paths, project, items)
    render.emit_rows(claimed, add_mod.ADDED_COLUMNS, as_json=json)
    return 0
