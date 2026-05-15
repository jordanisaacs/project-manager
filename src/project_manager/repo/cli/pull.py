"""`pm repo pull`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.cli._params import RepoListArg, parse_repo_list
from project_manager.repo import pull as pull_mod

from . import repo_app


@repo_app.command
def pull(
    *,
    repo: RepoListArg = None,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Fetch + ff-only pull each canonical repo."""
    paths = config.load()
    repos = parse_repo_list(repo)
    results = pull_mod.pull(paths, repos)
    render.emit_rows(results, pull_mod.COLUMNS, as_json=json)
    return 1 if any(not r.ok for r in results) else 0
