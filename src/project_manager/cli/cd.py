"""`pm cd` — resolve and print the path to a project (or worktree).

`cd` is a shell builtin, so the python program can only print the
resolved path — the actual directory change happens in the
`integrations/pm-cd.zsh` shell wrapper, which captures stdout and runs
`builtin cd`. `--print` opts out of that wrapper for scripting.
"""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.errors import ProjectError
from project_manager.project import discovery

from ._shared import root


@root.command
def cd(
    project: str,
    wt: str | None = None,
    *,
    print_: Annotated[
        bool,
        Parameter(
            name="--print",
            negative="",
            help="print the path instead of cd'ing (opts out of the shell wrapper)",
        ),
    ] = False,
) -> int:
    """Change directory to a project, or one of its worktrees.

    With `integrations/pm-cd.zsh` sourced, `pm cd <project>` cd's into
    the project. Pass `--print` to print the path instead — useful in
    scripts (`cd "$(pm cd --print myproj)"`).
    """
    del print_  # consumed by the shell wrapper; python always prints.
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
            f"worktree '{wt}' is detached; run `pm project wt attach -p {project} --wt {wt}`",
        )
    print(forward)
    return 0
