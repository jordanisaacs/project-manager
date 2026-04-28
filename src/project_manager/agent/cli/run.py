"""Cyclopts stubs for `pm agent <claude|codex|cursor>`.

Each command is a three-line wrapper that dispatches an `AgentName`
enum value into the shared `run_mod.run` — keeps the dispatch
explicit and grep-able (`AgentName.CLAUDE` → `"claude"` command name)
while letting the real logic live in one place.

`Parameter(allow_leading_hyphen=True)` on the varargs is what lets
`*args` consume tokens starting with `-` (e.g., `--resume <uuid>`)
without cyclopts trying to interpret them as pm's own flags.
"""

from typing import Annotated

from cyclopts import Parameter

from project_manager.agent import run as run_mod
from project_manager.agent.run import AgentName
from project_manager.cli._params import ProjectArg

from . import agent_app

_Forwarded = Annotated[str, Parameter(allow_leading_hyphen=True)]

# Signature ordering matters: `*args` MUST come before the `-p`
# keyword-only parameter. Cyclopts otherwise tries to match a leading-
# hyphen forwarded token (e.g. `--resume`) against pm's known options
# before falling through to `*args`, and errors with "Unknown option"
# — even with `allow_leading_hyphen=True`. Keyword-only placement
# routes unknown leading-hyphen tokens to `*args` cleanly.
#
# `help_flags=""` disables cyclopts's built-in `--help` interception
# for these commands so `pm agent claude --help` forwards to the agent
# (pm's help is still available via `pm agent --help`).


@agent_app.command(name="claude", help_flags="")
def claude(*args: _Forwarded, project: ProjectArg = None) -> int:
    """Launch Claude Code in the pm project dir, forwarding extra args."""
    run_mod.run(AgentName.CLAUDE, project, args)
    return 0  # unreachable; os.execvp replaces the process on success


@agent_app.command(name="codex", help_flags="")
def codex(*args: _Forwarded, project: ProjectArg = None) -> int:
    """Launch Codex CLI in the pm project dir, forwarding extra args."""
    run_mod.run(AgentName.CODEX, project, args)
    return 0


@agent_app.command(name="cursor", help_flags="")
def cursor(*args: _Forwarded, project: ProjectArg = None) -> int:
    """Launch Cursor agent in the pm project dir, forwarding extra args."""
    run_mod.run(AgentName.CURSOR, project, args)
    return 0
