"""`pm stacker reparent`."""
from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import RepoFlag

from . import _common, stacker_app


@stacker_app.command
def reparent(
    new_parent: str | None = None,
    *,
    branch: str | None = None,
    flag: RepoFlag = RepoFlag(),
    continue_: Annotated[
        bool,
        Parameter(name="--continue", negative="", help="resume a paused cherry-pick"),
    ] = False,
    abort: Annotated[
        bool,
        Parameter(negative="", help="abort the paused op"),
    ] = False,
) -> int:
    """Move current branch onto a new parent; cherry-pick descendants."""
    paths = config.load()
    svc = _common.service(paths)
    if continue_:
        return _common.emit(
            svc.continue_operation(_common.resolve_repo(flag.repo, paths))
        )
    if abort:
        return _common.emit(
            svc.abort_operation(_common.resolve_repo(flag.repo, paths))
        )
    if not new_parent:
        raise ValueError("reparent requires <new-parent> (or --continue / --abort).")
    target = _common.target(flag.repo, branch, paths)
    parent = _common.resolve_on_spec(paths, target.repo_name, new_parent)
    return _common.emit(svc.reparent(target, parent))
