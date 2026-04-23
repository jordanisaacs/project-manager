"""`pm project wt detach`."""
from project_manager import config
from project_manager.cli._shared import ProjectFlag, WtSelection, selected_wts
from project_manager.project import current
from project_manager.project import detach as detach_mod
from project_manager.project.cli._plan_printers import print_detach_plan

from . import wt_app


@wt_app.command
def detach(
    flag: ProjectFlag = ProjectFlag(),
    sel: WtSelection = WtSelection(),
    *,
    dry_run: bool = False,
) -> int:
    """Detach worktrees: unlink forward + release pool row.

    --dry-run prints the plan without mutating state; exits 1 if any blocker.
    """
    paths = config.load()
    project = current.resolve_project(paths, flag.project)
    selected = selected_wts(sel)
    if dry_run:
        plan = detach_mod.plan_detach(paths, project, selected)
        print_detach_plan(plan)
        return 1 if plan.has_blocker else 0
    released = detach_mod.detach(paths, project, selected)
    for d in released:
        print(f"{d.wt}\t{d.path}")
    return 0
