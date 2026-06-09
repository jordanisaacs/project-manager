"""Shared dry-run plan printers for project delete, wt remove, and detach."""

import sys

from project_manager import render
from project_manager.paths import Paths
from project_manager.project import delete as delete_mod
from project_manager.project import detach as detach_mod


def emit_detach_plan(plan: detach_mod.DetachPlan, *, as_json: bool) -> None:
    if as_json:
        render.emit_json(plan.__pm_json__())
        return
    render.emit_rows(plan.actions, detach_mod.DETACH_ACTION_COLUMNS)
    # Blockers also go to stderr so `pm … --dry-run 2>err.log` surfaces them
    # separately from the plan table.
    for action in plan.actions:
        if action.blocker is not None:
            print(f"pm: BLOCKED: {action.wt}: {action.blocker}", file=sys.stderr)


def emit_delete_plan(
    plan: delete_mod.DeletePlan,
    paths: Paths,
    *,
    as_json: bool,
) -> None:
    if as_json:
        render.emit_json(plan.__pm_json__())
        return
    for extra in plan.extras:
        print(f"pm: BLOCKED: non-pm entry {extra}", file=sys.stderr)
    if plan.detach_plan.actions:
        emit_detach_plan(plan.detach_plan, as_json=False)
    if plan.drop_rows:
        print()
        render.console().print(
            f"Would drop {len(plan.drop_rows)} db row(s): " + ", ".join(plan.drop_rows),
        )
    extras: list[str] = []
    if plan.remove_readme:
        extras.append(f"remove {paths.project(plan.project) / 'README.md'}")
    if plan.drop_db:
        extras.append(f"drop {paths.project_db(plan.project)}")
    if plan.rmdir:
        extras.append(f"rmdir {paths.project(plan.project)}")
    for e in extras:
        print(f"Would {e}")
