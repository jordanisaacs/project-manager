from __future__ import annotations

from pathlib import Path

from project_manager.paths import Paths

from . import git


def locate_worktree(paths: Paths, repo_name: str, branch: str) -> Path | None:
    """Return the worktree path where `branch` is currently checked out, or None.

    Source of truth: `git worktree list --porcelain` in paths.repo(repo_name).
    Git enforces at-most-one-worktree-per-branch, so the result is 0-or-1.
    """
    repo = paths.repo(repo_name)
    if not repo.is_dir():
        return None
    for info in git.worktree_list(str(repo)):
        if info.branch == branch:
            return Path(info.path)
    return None
