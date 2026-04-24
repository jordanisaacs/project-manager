"""`pm` cyclopts entrypoint.

Sub-apps are registered by importing the per-group modules below for their
side effects. `main()` is the console-script target; it funnels
`ProjectError` / `CommandError` (the two error types commands raise) into
the standard `pm: <msg>` stderr line and exit code 2.
"""
import sys

from project_manager.agent.cli import agent_app as _agent_app  # noqa: F401
from project_manager.errors import CommandError, ProjectError
from project_manager.pool.cli import pool_app as _pool_app  # noqa: F401
from project_manager.pool.slot import PoolExhaustedError
from project_manager.project.cli import project_app as _project_app  # noqa: F401

# Group sub-apps (registration happens at import time).
from project_manager.repo.cli import repo_app as _repo_app  # noqa: F401
from project_manager.stacker.commands import stacker_app as _stacker_app  # noqa: F401

from . import _complete as _complete  # registers `__complete`
from . import check as _check  # noqa: F401  — registers the `check` command
from ._shared import fail, root


def main(argv: list[str] | None = None) -> int:
    try:
        result = root(argv, result_action="return_value")
    except (ProjectError, CommandError, PoolExhaustedError, ValueError) as e:
        return fail(str(e))
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":
    sys.exit(main())
