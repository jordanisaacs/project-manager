"""`pm project create`."""
from project_manager import config
from project_manager.project import create as create_mod
from project_manager.project.spec import parse_wt_spec

from . import project_app


@project_app.command
def create(project: str, *, wt: str | None = None) -> int:
    """Create a project (optionally with initial worktrees).

    --wt takes a comma-separated spec: `<repo>` or `<name>:<repo>`.
    """
    paths = config.load()
    spec = parse_wt_spec(wt) if wt else []
    claimed = create_mod.create(paths, project, spec)
    for wt_name, slot in claimed:
        print(f"{wt_name}\t{slot.path}")
    return 0
