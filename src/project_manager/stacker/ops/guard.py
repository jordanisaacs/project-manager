from __future__ import annotations

from typing import TYPE_CHECKING

from project_manager.stacker import git, selectors

from . import worktree

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def guard_no_rebase(ctx: StackerCtx) -> None:
    """Hook entry point: error out if cwd is on a stacker-tracked branch.

    Fails open: if cwd isn't in a pm worktree, isn't in any git repo, or is
    on a detached HEAD, we can't identify a tracked branch and must let
    git proceed — the guard only blocks when it's certain.
    """
    try:
        context = git.current_context()
    except git.GitError:
        return
    repo_name = worktree.repo_name_for_path(ctx, context.worktree_path)
    if repo_name is None:
        return
    tracked = ctx.db.get_branch(repo_name, context.branch)
    if not tracked:
        return
    label = selectors.selector_for(repo_name, context.branch)
    if ctx.db.get_operation(repo_name):
        raise git.GitError(
            f"{label} is managed by pm stacker and has an active operation. "
            "Do not rebase it; use `pm stacker continue` or `pm stacker abort`."
        )
    raise git.GitError(
        f"{label} is managed by pm stacker. "
        "Do not rebase it; use `pm stacker sync` or `pm stacker push` instead."
    )
