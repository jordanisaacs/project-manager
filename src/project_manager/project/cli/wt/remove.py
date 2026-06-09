"""`pm project wt remove`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import ProjectFlag, WtSelection, selected_wts
from project_manager.project import current
from project_manager.project import remove as remove_mod
from project_manager.project.cli._plan_printers import emit_remove_plan

from . import wt_app


@wt_app.command
def remove(
    flag: ProjectFlag = ProjectFlag(),
    sel: WtSelection = WtSelection(),
    *,
    dry_run: bool = False,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Remove worktree row(s) from a project.

    --dry-run prints the plan without mutating state; exits 1 if any blocker.
    """
    paths = config.load()
    project = current.resolve_project(paths, flag.project)
    selected = selected_wts(sel)
    if dry_run:
        plan = remove_mod.plan_remove(paths, project, selected)
        emit_remove_plan(plan, paths, as_json=json)
        return 1 if plan.has_blocker else 0
    remove_mod.remove(paths, project, selected)
    return 0
