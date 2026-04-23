"""`pm project ls`."""
from project_manager import config
from project_manager.project import ls as ls_mod

from . import project_app


@project_app.command
def ls() -> int:
    """List projects."""
    paths = config.load()
    for row in ls_mod.ls(paths):
        print(
            f"{row.project}\t{row.wt}\t{row.repo}\t{row.slot_uuid}\t{row.status}"
        )
    return 0
