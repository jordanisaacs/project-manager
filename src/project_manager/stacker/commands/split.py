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
) -> int:
    """Move commits [<commit>..HEAD] onto a new child branch.

    --stay: skip claiming a pool slot for the new branch (ref + DB row still land).
    """
    paths = config.load()
    svc = _common.service(paths)
    return _common.emit(
        svc.split(_common.target(flag.repo, branch, paths), new_name, commit, stay=stay)
    )
