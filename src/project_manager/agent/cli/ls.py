"""`pm agent ls`."""

import asyncio
import sys
from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render, tui
from project_manager.agent import ls as ls_mod
from project_manager.agent import run as run_mod
from project_manager.agent.run import AgentName
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
    resume: Annotated[
        bool,
        Parameter(
            negative="",
            help="pick a session interactively and resume it",
        ),
    ] = False,
) -> int:
    """List recent chat sessions across Claude / Codex / Cursor, per project.

    With no flags and a cwd inside a pm project, shows that project
    only. Outside any project, shows every project with sessions. Use
    `--project` or `--all` to override; `--agent claude,codex` narrows
    to a subset of agents. `--limit` defaults to 15 for a single project
    and 5 when listing multiple projects.

    `--resume` turns the listing into an interactive picker: navigate
    with arrow / vim / emacs keys, Enter to resume the selected
    session, Esc to cancel. Requires a TTY and cannot be combined
    with `--json`.
    """
    if resume and json:
        raise ValueError("--resume cannot be combined with --json")
    if resume and (not sys.stdin.isatty() or not sys.stderr.isatty()):
        raise ValueError("--resume requires an interactive terminal")
    paths = config.load()
    projects = resolve_project_scope(paths, scope)
    agents = parse_agents(agent)
    effective_limit = (
        limit
        if limit is not None
        else (_DEFAULT_LIMIT_SINGLE if len(projects) == 1 else _DEFAULT_LIMIT_MULTI)
    )
    rows = asyncio.run(ls_mod.ls(paths, projects, effective_limit, agents))
    if resume:
        return _resume(rows)
    render.emit_sections(
        ls_mod.sections(rows),
        ls_mod.COLUMNS,
        group=render.GroupColumn("Project"),
        as_json=json,
        shape=render.JsonShape("project", "sessions"),
    )
    return 0


def _resume(rows: list[ls_mod.AgentRow]) -> int:
    """Run the interactive picker, then `execvp` into the chosen agent.

    Placeholder rows (`ls._placeholder` sentinels for projects with no
    sessions) still render so the picker's table matches the
    non-interactive view, but are filtered out of navigation via
    `is_selectable`. When every row is a placeholder, there is nothing
    to resume — we print a friendly stderr note and exit 0.
    """
    sections = ls_mod.sections(rows)
    if not any(r.session_id for r in rows):
        render.console(stderr=True).print("no sessions to resume")
        return 0
    chosen = tui.pick(
        sections,
        ls_mod.COLUMNS,
        group=render.GroupColumn("Project"),
        header="Select a session to resume",
        is_selectable=lambda r: bool(r.session_id),
    )
    if chosen is None:
        return 0
    agent_name = AgentName(chosen.agent)
    # `run_mod.run` is annotated `NoReturn` — os.execvp replaces the
    # process — but the ruff `return`-completeness rule doesn't see
    # that, so we return an exit code for form's sake after the call.
    run_mod.run(
        agent_name,
        chosen.project,
        run_mod.resume_args(agent_name, chosen.session_id),
    )
    return 0
