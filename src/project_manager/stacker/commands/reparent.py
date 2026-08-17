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
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Move current branch onto a new parent; cherry-pick descendants.

    The `--allow-drop-*` and `--offline` flags are forwarded to the
    downstream sync (see `pm stacker sync`).
    An explicit `--branch` need not be checked out; downstream sync locates
    or acquires the worktrees it mutates.
    """
    paths = config.load()
    svc = _common.service(paths)
    if continue_ or abort:
        if allow_drop_parent_modifications or allow_drop_merge or offline:
            raise git.GitError(
                "--allow-drop-* and --offline cannot be combined with --continue or --abort.",
            )
        repo = _common.resolve_repo(flag.repo, paths)
        result = svc.continue_operation(repo) if continue_ else svc.abort_operation(repo)
        return _common.emit_result(
            result,
            json=json,
            command="continue" if continue_ else "abort",
            repo=repo,
            branch=None,
            paths=paths,
        )
    if not new_parent:
        raise ValueError("reparent requires <new-parent> (or --continue / --abort).")
    target = _common.target(flag.repo, branch, paths)
    parent = _common.resolve_on_spec(paths, target.repo_name, new_parent)
    return _common.emit_result(
        svc.reparent(
            target,
            parent,
            options=SyncOptions(
                allow_drop_parent_modifications=allow_drop_parent_modifications,
                allow_drop_merge=allow_drop_merge,
                offline=offline,
            ),
        ),
        json=json,
        command="reparent",
        repo=target.repo_name,
        branch=target.branch,
        paths=paths,
    )
