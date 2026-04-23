"""`pm project delete`."""
from project_manager import config
from project_manager.project import delete as delete_mod

from . import project_app
from ._plan_printers import print_delete_plan


@project_app.command
def delete(project: str, *, dry_run: bool = False) -> int:
    """Delete a whole project.

    --dry-run prints the plan without mutating state; exits 1 if any blocker.
    """
    paths = config.load()
    if dry_run:
        plan = delete_mod.plan_delete(paths, project, None)
        print_delete_plan(plan, paths)
        return 1 if plan.has_blocker else 0
    delete_mod.delete(paths, project, None)
    return 0
