"""`pm pool gc-ops`."""
from project_manager import config
from project_manager.stacker.gc import gc_ops as gc_ops_mod

from . import pool_app


@pool_app.command
def gc_ops() -> int:
    """Release stacker-ops slots that have no live operation."""
    paths = config.load()
    for slot in gc_ops_mod(paths):
        print(f"{slot.repo}\t{slot.uuid}\t{slot.path}")
    return 0
