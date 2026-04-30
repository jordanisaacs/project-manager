"""`pm stacker sync`."""
from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import StackerScope
from project_manager.stacker import git

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
    hard: Annotated[
        bool,
        Parameter(
            negative="",
            help="cherry-pick exactly the commits added since last sync; "
            "skip patch-id dedup. Use when the parent was rewritten in place.",
        ),
    ] = False,
) -> int:
    """Cherry-pick branches onto their parents.

    --continue / --abort are shortcuts for `pm stacker continue` / `abort`.
    --hard replays only the commits the child added on top of its recorded
    base — bypasses patch-id dedup so a rewritten parent doesn't cause a
    superseded duplicate to be replayed.
    """
    paths = config.load()
    svc = _common.service(paths)
    if continue_ or abort:
        if hard:
            raise git.GitError("--hard cannot be combined with --continue or --abort.")
        if continue_:
            return _common.emit(svc.continue_operation(_common.resolve_repo(scope.repo, paths)))
        return _common.emit(svc.abort_operation(_common.resolve_repo(scope.repo, paths)))
    target = _common.target(scope.repo, branch, paths)
    return _common.emit(svc.sync(target, _common.scope_spec(scope), hard=hard))
