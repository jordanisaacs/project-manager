"""`pm agent ls`."""
import asyncio
from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.agent import ls as ls_mod
from project_manager.agent.sources import parse_agents
from project_manager.cli._shared import ProjectOrAllScope, resolve_project_scope

from . import agent_app

_DEFAULT_LIMIT_SINGLE = 15
_DEFAULT_LIMIT_MULTI = 5


@agent_app.command
def ls(
    scope: ProjectOrAllScope = ProjectOrAllScope(),
    *,
    limit: Annotated[int | None, Parameter(name=("-n", "--limit"))] = None,
    agent: Annotated[
        str | None,
        Parameter(help="comma-separated subset: claude,codex,cursor"),
    ] = None,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """List recent chat sessions across Claude / Codex / Cursor, per project.

    With no flags and a cwd inside a pm project, shows that project
    only. Outside any project, shows every project with sessions. Use
    `--project` or `--all` to override; `--agent claude,codex` narrows
    to a subset of agents. `--limit` defaults to 15 for a single project
    and 5 when listing multiple projects.
    """
    paths = config.load()
    projects = resolve_project_scope(paths, scope)
    agents = parse_agents(agent)
    effective_limit = limit if limit is not None else (
        _DEFAULT_LIMIT_SINGLE if len(projects) == 1 else _DEFAULT_LIMIT_MULTI
    )
    rows = asyncio.run(ls_mod.ls(paths, projects, effective_limit, agents))
    render.emit_sections(
        ls_mod.sections(rows), ls_mod.COLUMNS,
        group=render.GroupColumn("Project"),
        as_json=json, shape=render.JsonShape("project", "sessions"),
    )
    return 0
