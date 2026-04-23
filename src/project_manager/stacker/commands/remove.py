"""`pm stacker remove`."""
from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import RepoFlag

from . import _common, stacker_app


@stacker_app.command
def remove(
    branch: str | None = None,
    flag: RepoFlag = RepoFlag(),
    *,
    force: Annotated[bool, Parameter(negative="")] = False,
    parent: Annotated[bool, Parameter(negative="")] = False,
    keep_branch: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Untrack a branch (optionally delete it); reparent children.

    --parent also removes all tracked ancestors. --keep-branch untracks
    without deleting the git branch (old `untrack` behavior).
    """
    paths = config.load()
    svc = _common.service(paths)
    return _common.emit(
        svc.remove(
            _common.target(flag.repo, branch, paths),
            keep_branch=keep_branch,
            parent_cascade=parent,
            force=force,
        )
    )
