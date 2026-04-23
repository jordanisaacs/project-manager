"""`pm pool add`."""
from project_manager import config
from project_manager.pool import add as add_mod

from . import pool_app


@pool_app.command
def add(repo: str) -> int:
    """Mint a new pool slot."""
    paths = config.load()
    slot = add_mod.add(paths, repo)
    print(f"{slot.repo}\t{slot.uuid}\t{slot.path}")
    return 0
