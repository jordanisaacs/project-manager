"""`pm stacker rename`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import RepoFlag

from . import _common, stacker_app


@stacker_app.command
def rename(
    new_name: str,
    *,
    branch: str | None = None,
    flag: RepoFlag = RepoFlag(),
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Rename the current branch.

    --branch picks a different source branch (defaults to cwd's current branch).
    """
    paths = config.load()
    svc = _common.service(paths)
    target = _common.target(flag.repo, branch, paths)
    return _common.emit_result(
        svc.rename(target, new_name),
        json=json,
        command="rename",
        repo=target.repo_name,
        branch=target.branch,
        paths=paths,
    )
