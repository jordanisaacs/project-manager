"""`pm pool delete`."""

import sys
from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.pool import delete as delete_mod

from . import pool_app


@pool_app.command
def delete(
    repo: str,
    uuid: str,
    *,
    dry_run: Annotated[
        bool,
        Parameter(help="show safety checks and planned deletion without changing anything"),
    ] = False,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Delete one exact, unclaimed pool slot.

    The slot must be a clean, unlocked Git worktree whose HEAD is reachable
    from a ref. Use `pm pool ls --json` to obtain the repo and full UUID.
    `--dry-run` reports blockers without mutating state.
    """
    paths = config.load()
    if dry_run:
        plan = delete_mod.plan_delete(paths, repo, uuid)
        if json:
            render.emit_json(plan.__pm_json__())
        else:
            render.emit_rows([plan], delete_mod.PLAN_COLUMNS)
            if plan.blocker is not None:
                print(f"pm: BLOCKED: {repo}/{uuid}: {plan.blocker}", file=sys.stderr)
        return 1 if plan.has_blocker else 0
    deleted = delete_mod.delete(paths, repo, uuid)
    render.emit_rows([deleted], delete_mod.DELETED_COLUMNS, as_json=json)
    return 0
