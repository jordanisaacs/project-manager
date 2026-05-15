"""`pm stacker reparent`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import RepoFlag
from project_manager.stacker import git
from project_manager.stacker.models import SyncOptions

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
    allow_drop_parent_modifications: Annotated[
        bool,
        Parameter(
            negative="",
            help="drop out-of-band history changes below the stack range and "
            "replay only working commits onto the new parent.",
        ),
    ] = False,
    allow_drop_merge: Annotated[
        bool,
        Parameter(
            negative="",
            help="when a descendant's PR is merged but its squash is not on "
            "its parent, drop the descendant's commits and reset to parent.",
        ),
    ] = False,
    offline: Annotated[
        bool,
        Parameter(
            negative="",
            help="skip the PR-state refresh; use cached pr_state for the merged-PR collapse.",
        ),
    ] = False,
) -> int:
    """Move current branch onto a new parent; cherry-pick descendants.

    The `--allow-drop-*` and `--offline` flags are forwarded to the
    downstream sync (see `pm stacker sync`).
    """
    paths = config.load()
    svc = _common.service(paths)
    if continue_ or abort:
        if allow_drop_parent_modifications or allow_drop_merge or offline:
            raise git.GitError(
                "--allow-drop-* and --offline cannot be combined with --continue or --abort.",
            )
        if continue_:
            return _common.emit(svc.continue_operation(_common.resolve_repo(flag.repo, paths)))
        return _common.emit(svc.abort_operation(_common.resolve_repo(flag.repo, paths)))
    if not new_parent:
        raise ValueError("reparent requires <new-parent> (or --continue / --abort).")
    target = _common.target(flag.repo, branch, paths)
    parent = _common.resolve_on_spec(paths, target.repo_name, new_parent)
    return _common.emit(
        svc.reparent(
            target,
            parent,
            options=SyncOptions(
                allow_drop_parent_modifications=allow_drop_parent_modifications,
                allow_drop_merge=allow_drop_merge,
                offline=offline,
            ),
        )
    )
