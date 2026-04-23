"""`pm project wt create`."""
from project_manager import config
from project_manager.cli._shared import ProjectFlag
from project_manager.project import create as create_mod
from project_manager.project import current
from project_manager.project.spec import parse_wt_spec

from . import wt_app


@wt_app.command
def create(spec: str, flag: ProjectFlag = ProjectFlag()) -> int:
    """Add worktree(s) to an existing project.

    <spec> is the same format as `pm project create --wt`:
    comma-separated `<repo>` or `<name>:<repo>`.
    """
    paths = config.load()
    project = current.resolve_project(paths, flag.project)
    items = parse_wt_spec(spec)
    claimed = create_mod.create(paths, project, items)
    for wt_name, slot in claimed:
        print(f"{wt_name}\t{slot.path}")
    return 0
