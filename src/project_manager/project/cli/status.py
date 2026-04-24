"""`pm project status`."""
import asyncio
from typing import Annotated

from cyclopts import Parameter

from project_manager import check as check_mod
from project_manager import config, render
from project_manager.agent import ls as agent_ls
from project_manager.agent.sources import REGISTRY
from project_manager.project import current
from project_manager.project import status as status_mod

from . import project_app

_SESSIONS_LIMIT = 15


@project_app.command
def status(
    project: str | None = None,
    *,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Show worktree health, PR state, stacker arms, and recent agent sessions."""
    paths = config.load()
    resolved = current.resolve_project(paths, project)
    ps = status_mod.status(paths, resolved)
    sessions = asyncio.run(
        agent_ls.ls(paths, [resolved], _SESSIONS_LIMIT, frozenset(REGISTRY)),
    )
    if json:
        render.emit_json(
            {
                "worktrees": [r.__pm_json__() for r in ps.worktrees],
                "prs": [r.__pm_json__() for r in ps.prs],
                "stacker": [r.__pm_json__() for r in ps.stacker],
                "sessions": [r.__pm_json__() for r in sessions],
            },
        )
    else:
        render.emit_markup("[bold]Worktrees[/bold]")
        render.emit_sections(
            status_mod.worktree_sections(ps.worktrees),
            status_mod.WORKTREE_COLUMNS,
            group=render.GroupColumn("Repo"),
        )
        if ps.prs:
            render.emit_markup("\n[bold]PRs[/bold]")
            render.emit_rows(ps.prs, status_mod.PR_COLUMNS)
        if ps.stacker:
            render.emit_markup("\n[bold]Stacker[/bold]")
            render.emit_rows(ps.stacker, status_mod.STACKER_COLUMNS)
        render.emit_markup("\n[bold]Recent sessions[/bold]")
        render.emit_sections(
            agent_ls.sections(sessions),
            agent_ls.COLUMNS,
            group=render.GroupColumn("Project"),
        )
    non_healthy = [r for r in ps.worktrees if r.finding.kind != check_mod.Kind.ACTIVE]
    return 1 if non_healthy else 0
