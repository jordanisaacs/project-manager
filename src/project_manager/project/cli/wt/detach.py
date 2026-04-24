"""`pm project wt detach`."""
from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.cli._shared import ProjectFlag, WtSelection, selected_wts
from project_manager.project import current
from project_manager.project import detach as detach_mod
from project_manager.project.cli._plan_printers import emit_detach_plan

from . import wt_app


@wt_app.command
def detach(
    flag: ProjectFlag = ProjectFlag(),
    sel: WtSelection = WtSelection(),
    *,
    dry_run: bool = False,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Detach worktrees: unlink forward + release pool row.

    --dry-run prints the plan without mutating state; exits 1 if any blocker.
    """
    paths = config.load()
    project = current.resolve_project(paths, flag.project)
    selected = selected_wts(sel)
    if dry_run:
        plan = detach_mod.plan_detach(paths, project, selected)
        emit_detach_plan(plan, as_json=json)
        return 1 if plan.has_blocker else 0
    released = detach_mod.detach(paths, project, selected)
    render.emit_rows(released, detach_mod.DETACHED_COLUMNS, as_json=json)
    return 0
