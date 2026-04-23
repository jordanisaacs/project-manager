"""`pm stacker sync`."""
from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import StackerScope

from . import _common, stacker_app


@stacker_app.command
def sync(
    branch: str | None = None,
    scope: StackerScope = StackerScope(),
    *,
    continue_: Annotated[
        bool,
        Parameter(name="--continue", negative="", help="resume a paused cherry-pick"),
    ] = False,
    abort: Annotated[
        bool,
        Parameter(negative="", help="abort the paused op"),
    ] = False,
) -> int:
    """Cherry-pick branches onto their parents.

    --continue / --abort are shortcuts for `pm stacker continue` / `abort`.
    """
    paths = config.load()
    svc = _common.service(paths)
    if continue_:
        return _common.emit(svc.continue_operation(_common.resolve_repo(scope.repo, paths)))
    if abort:
        return _common.emit(svc.abort_operation(_common.resolve_repo(scope.repo, paths)))
    target = _common.target(scope.repo, branch, paths)
    return _common.emit(svc.sync(target, _common.scope_spec(scope)))
