"""`pm pool add`."""
from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.pool import add as add_mod
from project_manager.pool.slot import SLOT_COLUMNS

from . import pool_app


@pool_app.command
def add(
    repo: str,
    *,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Mint a new pool slot."""
    paths = config.load()
    slot = add_mod.add(paths, repo)
    render.emit_rows([slot], SLOT_COLUMNS, as_json=json)
    return 0
