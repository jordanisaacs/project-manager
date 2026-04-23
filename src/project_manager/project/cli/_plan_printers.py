"""Shared dry-run plan printers for project/wt delete and detach."""
import sys

from project_manager.paths import Paths
from project_manager.project import delete as delete_mod
from project_manager.project import detach as detach_mod


def print_detach_plan(plan: detach_mod.DetachPlan) -> None:
    for action in plan.actions:
        if action.blocker is not None:
            print(
                f"dry-run: would detach {action.wt}\tBLOCKED: {action.blocker}",
                file=sys.stderr,
            )
            continue
        if action.kind == "noop":
            print(f"dry-run: {action.wt} already detached")
            continue
        print(f"dry-run: would detach {action.wt}\trelease slot {action.slot_uuid}")


def print_delete_plan(plan: delete_mod.DeletePlan, paths: Paths) -> None:
    for extra in plan.extras:
        print(f"dry-run: BLOCKED: non-pm entry {extra}", file=sys.stderr)
    print_detach_plan(plan.detach_plan)
    for wt in plan.drop_rows:
        print(f"dry-run: would drop db row {wt}")
    if plan.remove_readme:
        print(f"dry-run: would remove {paths.project(plan.project) / 'README.md'}")
    if plan.drop_db:
        print(f"dry-run: would drop {paths.project_db(plan.project)}")
    if plan.rmdir:
        print(f"dry-run: would rmdir {paths.project(plan.project)}")
