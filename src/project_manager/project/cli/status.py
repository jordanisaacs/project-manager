"""`pm project status`."""
from typing import Annotated

from cyclopts import Parameter

from project_manager import check as check_mod
from project_manager import config, render
from project_manager.project import current
from project_manager.project import status as status_mod

from . import project_app


@project_app.command
def status(
    project: str | None = None,
    *,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Show health + db rows for a single project (defaults to current)."""
    paths = config.load()
    resolved = current.resolve_project(paths, project)
    rows = status_mod.status(paths, resolved)
    render.emit_sections(
        status_mod.sections(rows), status_mod.COLUMNS,
        group=render.GroupColumn("Worktree"),
        as_json=json, shape=render.JsonShape("worktree", "findings"),
    )
    non_healthy = [r for r in rows if r.finding.kind != check_mod.Kind.ACTIVE]
    return 1 if non_healthy else 0
