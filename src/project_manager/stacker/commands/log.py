"""`pm stacker log`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import RepoFlag

from . import _common, stacker_app


@stacker_app.command
def log(
    branch: str | None = None,
    flag: RepoFlag = RepoFlag(),
    *,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Show commits since the branch's managed base.

    `--branch` reads the named ref directly; it need not be checked out and
    the current worktree may be on another branch or detached.
    """
    paths = config.load()
    svc = _common.service(paths)
    target = _common.target(flag.repo, branch, paths)
    return _common.emit_result(
        svc.log_text(target),
        json=json,
        command="log",
        repo=target.repo_name,
        branch=target.branch,
        paths=paths,
    )
