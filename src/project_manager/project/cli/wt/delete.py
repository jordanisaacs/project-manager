"""`pm project wt delete`."""
from project_manager import config
from project_manager.cli._shared import ProjectFlag, WtSelection, selected_wts
from project_manager.project import current
from project_manager.project import delete as delete_mod
from project_manager.project.cli._plan_printers import print_delete_plan

from . import wt_app


@wt_app.command
def delete(
    flag: ProjectFlag = ProjectFlag(),
    sel: WtSelection = WtSelection(),
    *,
    dry_run: bool = False,
) -> int:
    """Delete worktree row(s) from a project.

    --dry-run prints the plan without mutating state; exits 1 if any blocker.
    """
    paths = config.load()
    project = current.resolve_project(paths, flag.project)
    selected = selected_wts(sel)
    if dry_run:
        plan = delete_mod.plan_delete(paths, project, selected)
        print_delete_plan(plan, paths)
        return 1 if plan.has_blocker else 0
    delete_mod.delete(paths, project, selected)
    return 0
