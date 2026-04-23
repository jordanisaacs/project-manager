"""Hidden `pm __complete` sub-app — value providers for shell completion.

Each verb prints one candidate per line to stdout and exits 0. No stderr,
no exceptions: completion runs on every keypress and must never disturb
the user's prompt.

Stdout contract (internal but stable — `integrations/_pm` parses it):
    <value>            # bare
    <value>:<desc>     # with zsh short-description

This module exists because cyclopts (as of v4.10) has no hook for dynamic
per-parameter completion. See upstream issue #641. When that lands, the
bodies below collapse into plain `() -> list[str]` functions attached via
`Parameter(completion=...)` and this sub-app can be deleted.
"""
from contextlib import suppress

from cyclopts import App

from project_manager import config
from project_manager.cli._shared import root
from project_manager.project import current, discovery
from project_manager.project import db as project_db
from project_manager.repo import ls as repo_ls

complete_app = root.command(
    App(name="__complete", show=False, help=""),
)


@complete_app.command(show=False)
def projects() -> int:
    """Project names under `paths.projects`."""
    with suppress(Exception):
        paths = config.load()
        for name, _ in discovery.list_project_dbs(paths):
            print(name)
    return 0


@complete_app.command(show=False)
def repos() -> int:
    """Canonical repo names under `paths.repos`."""
    with suppress(Exception):
        paths = config.load()
        for row in repo_ls.ls(paths):
            print(row.repo)
    return 0


@complete_app.command(show=False)
def worktrees(*, project: str | None = None) -> int:
    """Worktree names for a project (defaults to the cwd's project)."""
    with suppress(Exception):
        paths = config.load()
        resolved = current.resolve_project(paths, project)
        db_path = discovery.require_project_db(paths, resolved)
        with project_db.readonly(db_path) as conn:
            for wt, _, _ in project_db.list_wts(conn):
                print(wt)
    return 0
