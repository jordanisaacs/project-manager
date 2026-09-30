"""`pm project delete`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.omnigent.sync import best_effort_sync
from project_manager.project import remove as remove_mod

from . import project_app
from ._plan_printers import emit_remove_plan


@project_app.command
def delete(
    project: str,
    *,
    dry_run: bool = False,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Delete a whole project.

    --dry-run prints the plan without mutating state; exits 1 if any blocker.
    """
    paths = config.load()
    if dry_run:
        plan = remove_mod.plan_remove(paths, project, None)
        emit_remove_plan(plan, paths, as_json=json)
        return 1 if plan.has_blocker else 0
    remove_mod.remove(paths, project, None)
    best_effort_sync(paths)
    return 0
