"""`pm stacker reparent`."""
from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import RepoFlag
from project_manager.stacker import git

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
    hard: Annotated[
        bool,
        Parameter(
            negative="",
            help="forward --hard to the downstream sync; skip patch-id dedup so "
            "a descendant whose shared commits have drifted from the new "
            "parent's equivalents replays only its own added commits.",
        ),
    ] = False,
) -> int:
    """Move current branch onto a new parent; cherry-pick descendants.

    --hard is forwarded to the downstream sync (see `pm stacker sync --hard`).
    """
    paths = config.load()
    svc = _common.service(paths)
    if continue_ or abort:
        if hard:
            raise git.GitError("--hard cannot be combined with --continue or --abort.")
        if continue_:
            return _common.emit(
                svc.continue_operation(_common.resolve_repo(flag.repo, paths))
            )
        return _common.emit(
            svc.abort_operation(_common.resolve_repo(flag.repo, paths))
        )
    if not new_parent:
        raise ValueError("reparent requires <new-parent> (or --continue / --abort).")
    target = _common.target(flag.repo, branch, paths)
    parent = _common.resolve_on_spec(paths, target.repo_name, new_parent)
    return _common.emit(svc.reparent(target, parent, hard=hard))
