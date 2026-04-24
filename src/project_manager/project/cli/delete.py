"""`pm project delete`."""
from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.project import delete as delete_mod

from . import project_app
from ._plan_printers import emit_delete_plan


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
        plan = delete_mod.plan_delete(paths, project, None)
        emit_delete_plan(plan, paths, as_json=json)
        return 1 if plan.has_blocker else 0
    delete_mod.delete(paths, project, None)
    return 0
