"""`pm pool ls`."""
from project_manager import config
from project_manager.pool import ls as ls_mod

from . import pool_app


@pool_app.command
def ls(repo: str | None = None) -> int:
    """List pool slots with claim status."""
    paths = config.load()
    for row in ls_mod.ls(paths, repo):
        print(f"{row.repo}\t{row.uuid}\t{row.status}")
    return 0
