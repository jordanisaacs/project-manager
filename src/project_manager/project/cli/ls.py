"""`pm project ls`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.project import discovery
from project_manager.project import ls as ls_mod

from . import project_app


@project_app.command
def ls(
    *,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """List projects, grouped by project name."""
    paths = config.load()
    rows = ls_mod.ls(paths)
    projects = [name for name, _ in discovery.list_project_dbs(paths)]
    render.emit_sections(
        ls_mod.sections(rows, projects),
        ls_mod.COLUMNS,
        group=render.GroupColumn("Project"),
        as_json=json,
        shape=render.JsonShape("project", "worktrees"),
    )
    return 0
