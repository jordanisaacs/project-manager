"""`pm repo maintenance`."""
from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.cli._params import RepoListArg, parse_repo_list
from project_manager.repo import maintenance as maintenance_mod

from . import repo_app


@repo_app.command
def maintenance(
    *,
    repo: RepoListArg = None,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Warm git object store + index + fsmonitor across repos and pool slots.

    Runs `git maintenance run` + `git worktree prune` on each canonical
    checkout, plus `git update-index --refresh` on the canonical repo and
    every pool slot under it. Excludes `prefetch` — fetching belongs to
    `pm repo pull`. Intended for a timer: keeps cold watchman crawls out
    of interactive commands.

    Concurrency is controlled by `[concurrency].limit` in config.toml
    (default 10).
    """
    paths = config.load()
    cfg = config.concurrency()
    repos = parse_repo_list(repo)
    results = maintenance_mod.maintain(paths, repos, cfg)
    render.emit_rows(results, maintenance_mod.COLUMNS, as_json=json)
    return 1 if any(not r.ok for r in results) else 0
