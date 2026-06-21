"""`pm stacker continue`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import RepoFlag

from . import _common, stacker_app


@stacker_app.command
def continue_(
    flag: RepoFlag = RepoFlag(),
    *,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Resume a paused stacker operation."""
    paths = config.load()
    svc = _common.service(paths)
    repo = _common.resolve_repo(flag.repo, paths)
    return _common.emit_result(
        svc.continue_operation(repo),
        json=json,
        command="continue",
        repo=repo,
        branch=None,
        paths=paths,
    )
