"""`pm stacker pr unlink` — clear cached PR association for a branch."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config, render
from project_manager.cli._shared import RepoFlag
from project_manager.stacker.commands import _common
from project_manager.stacker.commands.pr import pr_app


@pr_app.command
def unlink(
    branch: str | None = None,
    flag: RepoFlag = RepoFlag(),
    *,
    all_: Annotated[
        bool,
        Parameter(name="--all", negative="", help="clear PR links for every branch"),
    ] = False,
) -> int:
    """Clear the cached PR association for a branch (or --all branches).

    Only touches the local pr_state cache; the GitHub PR is untouched.
    The next push/sync will re-resolve the PR for the branch.
    """
    if all_ and branch is not None:
        raise ValueError("--all takes no branch argument.")
    paths = config.load()
    svc = _common.service(paths)
    repo_name = _common.resolve_repo(flag.repo, paths)
    out = render.console()
    if all_:
        n = svc.delete_all_pr_state(repo_name)
        out.print(f"Cleared {n} PR link(s).", markup=False, highlight=False)
        return 0
    target_branch = _common.resolve_branch(branch, paths)
    if svc.delete_pr_state(repo_name, target_branch):
        out.print(
            f"Cleared PR link for '{target_branch}'.",
            markup=False,
            highlight=False,
        )
    else:
        out.print(
            f"Branch '{target_branch}' has no PR link to clear.",
            markup=False,
            highlight=False,
        )
    return 0
