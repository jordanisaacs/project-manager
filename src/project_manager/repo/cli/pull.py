"""`pm repo pull`."""
from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.repo import pull as pull_mod

from . import repo_app


@repo_app.command
def pull(
    *,
    repo: str | None = None,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Fetch + ff-only pull each canonical repo.

    --repo accepts a comma-separated list; defaults to every repo.
    """
    paths = config.load()
    repos = [r.strip() for r in repo.split(",") if r.strip()] if repo else None
    results = pull_mod.pull(paths, repos)
    render.emit_rows(results, pull_mod.COLUMNS, as_json=json)
    return 1 if any(not r.ok for r in results) else 0
