"""`pm stacker split`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import RepoFlag

from . import _common, stacker_app


@stacker_app.command
def split(
    new_name: str,
    commit: str,
    *,
    branch: str | None = None,
    flag: RepoFlag = RepoFlag(),
    stay: Annotated[bool, Parameter(negative="")] = False,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Move commits [<commit>..HEAD] onto a new child branch.

    --stay: skip claiming a pool slot for the new branch (ref + DB row still land).
    The source branch must be checked out because split resets its worktree.
    """
    paths = config.load()
    svc = _common.service(paths)
    target = _common.target(flag.repo, branch, paths)
    return _common.emit_result(
        svc.split(target, new_name, commit, stay=stay),
        json=json,
        command="split",
        repo=target.repo_name,
        branch=target.branch,
        paths=paths,
    )
