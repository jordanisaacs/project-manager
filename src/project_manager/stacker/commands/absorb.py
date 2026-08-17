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
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Cherry-pick the current branch's new commits onto its parent.

    One level only. The parent's ref advances by the child's commits (minus
    any already on the parent by patch-id); the child branch is untouched.
    --continue / --abort are shortcuts for `pm stacker continue` / `abort`.
    An explicit `--branch` need not be checked out; stacker acquires the
    parent worktree that absorb mutates.
    """
    paths = config.load()
    svc = _common.service(paths)
    if continue_ or abort:
        repo = _common.resolve_repo(scope.repo, paths)
        result = svc.continue_operation(repo) if continue_ else svc.abort_operation(repo)
        return _common.emit_result(
            result,
            json=json,
            command="continue" if continue_ else "abort",
            repo=repo,
            branch=None,
            paths=paths,
        )
    target = _common.target(scope.repo, branch, paths)
    return _common.emit_result(
        svc.absorb(target),
        json=json,
        command="absorb",
        repo=target.repo_name,
        branch=target.branch,
        paths=paths,
    )
