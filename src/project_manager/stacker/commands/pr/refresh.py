"""`pm stacker pr refresh` — re-discover the GitHub PR for a tracked branch."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.cli._shared import RepoFlag
from project_manager.stacker.commands import _common
from project_manager.stacker.commands.pr import pr_app


@pr_app.command
def refresh(
    branch: str | None = None,
    flag: RepoFlag = RepoFlag(),
    *,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Look up the current GitHub PR for a tracked branch and cache it.

    Drops the existing cache row for the branch (if any) and runs a
    fresh `repo:X head:Y is:pr is:open` search via the gh backend, then
    persists the hit in the pr_state cache so `pm stacker ls` shows the
    URL. Useful right after `pm stacker create --replace`, or any time
    a PR was opened by another tool and you want stacker to know.
    """
    paths = config.load()
    svc = _common.service(paths)
    target = _common.target(flag.repo, branch, paths)
    pr = svc.refresh_pr(target)
    found = pr is not None
    message = (
        f"Linked '{target.branch}' to {pr.url}"
        if pr is not None
        else f"No open PR found for '{target.branch}'."
    )
    if json:
        return _common.emit_result(
            message,
            json=True,
            command="pr-refresh",
            repo=target.repo_name,
            branch=target.branch,
            paths=paths,
            ok=found,
            code=0 if found else 1,
        )
    render.console().print(message, markup=False, highlight=False)
    return 0 if found else 1
