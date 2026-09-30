"""`pm project create`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.omnigent.sync import best_effort_sync
from project_manager.project import add as add_mod
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
    claimed = add_mod.add(paths, project, spec)
    render.emit_rows(claimed, add_mod.ADDED_COLUMNS, as_json=json)
    best_effort_sync(paths)
    return 0
