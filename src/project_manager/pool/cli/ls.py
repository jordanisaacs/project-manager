"""`pm pool ls`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.cli._params import RepoOptionalArg
from project_manager.pool import ls as ls_mod

from . import pool_app


@pool_app.command
def ls(
    repo: RepoOptionalArg = None,
    *,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """List pool slots with claim status, grouped by repo."""
    paths = config.load()
    rows = ls_mod.ls(paths, repo)
    render.emit_sections(
        ls_mod.sections(rows),
        ls_mod.COLUMNS,
        group=render.GroupColumn("Repo"),
        as_json=json,
        shape=render.JsonShape("repo", "slots"),
    )
    return 0
