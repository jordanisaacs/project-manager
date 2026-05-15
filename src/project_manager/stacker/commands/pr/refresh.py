"""`pm stacker pr refresh` — re-discover the GitHub PR for a tracked branch."""

from project_manager import config, render
from project_manager.cli._shared import RepoFlag
from project_manager.stacker.commands import _common
from project_manager.stacker.commands.pr import pr_app


@pr_app.command
def refresh(
    branch: str | None = None,
    flag: RepoFlag = RepoFlag(),
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
    out = render.console()
    pr = svc.refresh_pr(target)
    if pr is None:
        out.print(
            f"No open PR found for '{target.branch}'.",
            markup=False,
            highlight=False,
        )
        return 1
    out.print(
        f"Linked '{target.branch}' to {pr.url}",
        markup=False,
        highlight=False,
    )
    return 0
