"""`pm stacker ls`."""
from typing import Annotated, Literal

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import StackerScope
from project_manager.stacker.models import Details

from . import _common, stacker_app

_DetailsLit = Literal["none", "status", "status-counts", "all"]


@stacker_app.command
def ls(
    branch: str | None = None,
    scope: StackerScope = StackerScope(),
    *,
    details: _DetailsLit = "status-counts",
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Render the stack tree."""
    paths = config.load()
    svc = _common.service(paths)
    current_scope = _common.scope_of(scope)
    details_cast: Details = details
    if current_scope == "current":
        target = _common.target(scope.repo, branch, paths)
        return _common.emit(
            svc.ls_text(
                target.repo_name,
                target_branch=target.branch,
                scope="current",
                details=details_cast,
                json_output=json,
            )
        )
    return _common.emit(
        svc.ls_text(
            _common.resolve_repo_optional(scope.repo, paths),
            scope="all",
            details=details_cast,
            json_output=json,
        )
    )
