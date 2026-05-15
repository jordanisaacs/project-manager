"""`pm stacker create`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import RepoFlag
from project_manager.stacker import git, selectors, slot
from project_manager.stacker.models import SelectorTarget, WorktreeInit

from . import _common, stacker_app


@stacker_app.command
def create(
    branch: str,
    *,
    flag: RepoFlag = RepoFlag(),
    on: str | None = None,
    copy: str | None = None,
    replace: Annotated[bool, Parameter(negative="")] = False,
    no_checkout: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Create or adopt a tracked branch (optionally in a pool slot).

    --on: parent — keyword 'current'/'parent', or a branch name (default: current).
    --copy: copy commits from this branch when creating the new one.
    --replace: adopt an existing branch instead of creating a new one.
    --no-checkout: do not claim a worktree slot; create the branch ref only.
    """
    paths = config.load()
    svc = _common.service(paths)
    repo_name = _common.resolve_repo(flag.repo, paths)
    on_spec = _on_spec_for_create(on, replace)
    parent = _common.resolve_on_spec(
        paths,
        repo_name,
        on_spec,
        fallback_branch=branch if replace else None,
    )
    if replace:
        target = SelectorTarget(repo_name=repo_name, branch=branch)
        tracked = svc.track(target, parent)
        print(selectors.selector_for(tracked.repo_name, tracked.branch))
        return 0
    if no_checkout:
        svc.create_tracked_branch(repo_name, branch, parent, copy_from=copy)
        print(selectors.selector_for(repo_name, branch))
        return 0
    acquired = slot.reserve_for_new_branch(svc.ctx, repo_name)
    try:
        svc.init_new_branch(
            WorktreeInit(
                repo_name=repo_name,
                worktree_path=acquired.path,
                branch=branch,
                parent=parent,
                copy_from=copy,
            )
        )
    except Exception:
        slot.release_if_owned(svc.ctx, acquired)
        raise
    print(acquired.path)
    return 0


def _on_spec_for_create(on: str | None, replace: bool) -> str:
    if on:
        return on
    if replace:
        raise git.GitError("--replace requires --on <parent-branch>.")
    return "current"
