"""`pm project wt attach`."""
from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.cli._shared import ProjectFlag, WtSelection, selected_wts
from project_manager.project import attach as attach_mod
from project_manager.project import current

from . import wt_app


@wt_app.command
def attach(
    flag: ProjectFlag = ProjectFlag(),
    sel: WtSelection = WtSelection(),
    *,
    no_branch: Annotated[bool, Parameter(negative="")] = False,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Re-attach worktrees (best-effort slot reclaim).

    --no-branch: skip saved-branch restore (leave slot in detached HEAD).
    """
    paths = config.load()
    project = current.resolve_project(paths, flag.project)
    result = attach_mod.attach(
        paths, project, selected_wts(sel), no_branch=no_branch,
    )
    err = render.console(stderr=True)
    for warning in result.warnings:
        err.print(f"pm: warn: {warning}", markup=False, highlight=False)
    render.emit_rows(result.newly_claimed, attach_mod.ATTACHED_COLUMNS, as_json=json)
    return 0
