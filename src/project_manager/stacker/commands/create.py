"""`pm stacker create`."""
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import RepoFlag
from project_manager.paths import Paths
from project_manager.pool.db import PoolDB
from project_manager.stacker import git, locate, ops_slot, selectors
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
    pooldb = PoolDB(paths.pool_db())
    repo_name = _common.resolve_repo(flag.repo, paths)
    on_spec = _on_spec_for_create(on, replace)
    parent = _common.resolve_on_spec(
        paths, repo_name, on_spec,
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
    worktree_path, cleanup = _resolve_create_slot(paths, pooldb, repo_name)
    try:
        svc.init_new_branch(
            WorktreeInit(
                repo_name=repo_name,
                worktree_path=worktree_path,
                branch=branch,
                parent=parent,
                copy_from=copy,
            )
        )
    except Exception:
        cleanup()
        raise
    print(worktree_path)
    return 0


def _on_spec_for_create(on: str | None, replace: bool) -> str:
    if on:
        return on
    if replace:
        raise git.GitError("--replace requires --on <parent-branch>.")
    return "current"


def _resolve_create_slot(
    paths: Paths, pooldb: PoolDB, repo_name: str,
) -> tuple[Path, Callable[[], None]]:
    """Pick the worktree to create the new branch in.

    Prefers the cwd slot when it's a pm slot for `repo_name`. Falls back
    to claiming a fresh ops slot. Returns (worktree_path, cleanup) —
    cleanup releases anything we claimed if the create fails; no-op when
    we reused an existing slot.
    """
    here = locate.slot_for_cwd(paths)
    if here is not None and here.repo_name == repo_name:
        return here.path, lambda: None
    claimed = ops_slot.claim(
        paths, pooldb, repo_name,
        wait=ops_slot.WaitOptions(progress=_common.stderr_progress),
    )
    return claimed.path, lambda: pooldb.release(claimed.repo, claimed.uuid)
