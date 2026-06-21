"""`pm stacker abort`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import RepoFlag

from . import _common, stacker_app


@stacker_app.command
def abort(
    flag: RepoFlag = RepoFlag(),
    *,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Abort a paused stacker operation and reset state."""
    paths = config.load()
    svc = _common.service(paths)
    repo = _common.resolve_repo(flag.repo, paths)
    return _common.emit_result(
        svc.abort_operation(repo),
        json=json,
        command="abort",
        repo=repo,
        branch=None,
        paths=paths,
    )
