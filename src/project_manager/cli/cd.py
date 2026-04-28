"""`pm cd` — print the path to a project (or one of its worktrees).

`cd` is a shell builtin, so this command can only print the resolved
path; the caller wraps it: `cd "$(pm cd myproj wt1)"`.
"""
from project_manager import config
from project_manager.errors import ProjectError
from project_manager.project import discovery

from ._shared import root


@root.command
def cd(project: str, wt: str | None = None) -> int:
    """Print the path of a project, or one of its worktrees.

    Usage:

        cd "$(pm cd myproj)"
        cd "$(pm cd myproj wt1)"
    """
    paths = config.load()
    discovery.require_project_db(paths, project)
    if wt is None:
        print(paths.project(project))
        return 0
    rows = discovery.read_wts(paths, project)
    if wt not in {w for w, _, _ in rows}:
        raise ProjectError(f"project '{project}' has no worktree '{wt}'")
    forward = paths.forward(project, wt)
    if not forward.is_symlink():
        raise ProjectError(
            f"worktree '{wt}' is detached; "
            f"run `pm project wt attach -p {project} --wt {wt}`",
        )
    print(forward)
    return 0
