"""`pm stacker absorb`."""
from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import RepoFlag

from . import _common, stacker_app


@stacker_app.command
def absorb(
    branch: str | None = None,
    scope: RepoFlag = RepoFlag(),
    *,
    continue_: Annotated[
        bool,
        Parameter(name="--continue", negative="", help="resume a paused absorb"),
    ] = False,
    abort: Annotated[
        bool,
        Parameter(negative="", help="abort the paused op"),
    ] = False,
) -> int:
    """Cherry-pick the current branch's new commits onto its parent.

    One level only. The parent's ref advances by the child's commits (minus
    any already on the parent by patch-id); the child branch is untouched.
    --continue / --abort are shortcuts for `pm stacker continue` / `abort`.
    """
    paths = config.load()
    svc = _common.service(paths)
    if continue_:
        return _common.emit(svc.continue_operation(_common.resolve_repo(scope.repo, paths)))
    if abort:
        return _common.emit(svc.abort_operation(_common.resolve_repo(scope.repo, paths)))
    target = _common.target(scope.repo, branch, paths)
    return _common.emit(svc.absorb(target))
