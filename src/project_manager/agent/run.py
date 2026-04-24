"""`pm agent <name>` — launch a coding-agent CLI in the pm project dir.

Three subcommands (`claude` / `codex` / `cursor`) share this module.
Their cyclopts handlers in `agent/cli/run.py` each dispatch one
`AgentName` into `run()`, which resolves the pm project, chdirs into
its dir, and `os.execvp`s the agent — replacing the pm Python process
with the agent's. Full process replacement (not `subprocess.run`)
lets the agent own the TTY without a capture layer that could corrupt
readline / color / cursor output.
"""

import os
import shlex
from enum import StrEnum
from pathlib import Path
from typing import NoReturn

from project_manager import config
from project_manager.errors import ProjectError
from project_manager.project import current


class AgentName(StrEnum):
    """Closed set of agents `pm agent <name>` can launch.

    `StrEnum` (not plain `Enum`) so `.value` equality with the dict
    keys coming out of `[agents.commands]` is natural and error
    messages print the canonical lowercase name. Adding a fourth
    agent needs (a) a new member here and (b) a matching cyclopts
    stub in `agent/cli/run.py` — both grep-visible.
    """

    CLAUDE = "claude"
    CODEX = "codex"
    CURSOR = "cursor"


def build_command(
    agent: AgentName,
    project_dir: Path,
    forwarded: tuple[str, ...],
    commands: dict[str, str],
) -> tuple[Path, list[str]]:
    """Return `(cwd, argv)` for the upcoming exec.

    Pure function — split from `run()` so tests can verify the argv
    shape without actually forking. `forwarded` is whatever cyclopts
    collected into the handler's `*args` (post `-p` flag). `commands`
    is the `[agents.commands]` table loaded by `config.agents()`.
    """
    prefix = commands.get(agent.value)
    if prefix is None:
        raise ProjectError(
            f"no [agents.commands].{agent.value} configured — "
            f"add it to ~/.config/pm/config.toml",
        )
    argv = [*shlex.split(prefix), *forwarded]
    if not argv:
        raise ProjectError(f"[agents.commands].{agent.value} is empty")
    return project_dir, argv


def run(
    agent: AgentName, project: str | None, forwarded: tuple[str, ...],
) -> NoReturn:
    """Resolve the project, chdir, and exec into the configured agent CLI.

    Never returns on success — `os.execvp` replaces the current
    process. A `FileNotFoundError` from exec (command not on PATH) is
    rewrapped as `ProjectError` so the top-level handler in
    `cli/__init__.py` renders it as the standard `pm: <msg>` line.
    """
    paths = config.load()
    commands = config.agents().commands
    project_name = current.resolve_project(paths, project)
    project_dir = paths.project(project_name)
    cwd, argv = build_command(agent, project_dir, forwarded, commands)
    os.chdir(cwd)
    try:
        # Exec without a shell is the whole point here — we want the
        # TTY to be handed to the agent binary directly, not through
        # `sh -c`. `argv[0]` is resolved against $PATH by execvp.
        os.execvp(argv[0], argv)  # noqa: S606
    except FileNotFoundError as e:
        raise ProjectError(
            f"agent command not found: {argv[0]!r} — "
            f"check [agents.commands].{agent.value} in config",
        ) from e
