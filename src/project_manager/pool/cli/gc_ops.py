"""`pm pool gc-ops`."""
from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.pool.slot import SLOT_COLUMNS
from project_manager.stacker.gc import gc_ops as gc_ops_mod

from . import pool_app


@pool_app.command
def gc_ops(
    *,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Release stacker-ops slots that have no live operation."""
    paths = config.load()
    slots = list(gc_ops_mod(paths))
    render.emit_rows(slots, SLOT_COLUMNS, as_json=json)
    return 0
