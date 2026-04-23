from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from project_manager.paths import Paths

from . import git


@dataclass(frozen=True)
class CurrentSlot:
    repo_name: str
    uuid: str
    path: Path


def locate_worktree(paths: Paths, repo_name: str, branch: str) -> Path | None:
    """Return the worktree path where `branch` is currently checked out, or None.

    Source of truth: `git worktree list --porcelain` in paths.repo(repo_name).
    Git enforces at-most-one-worktree-per-branch, so the result is 0-or-1.
    """
    repo = paths.repo(repo_name)
    if not repo.is_dir():
        return None
    for info in git.worktree_list(repo):
        if info.branch == branch:
            return info.path
    return None


def slot_for_cwd(paths: Paths, cwd: Path | None = None) -> CurrentSlot | None:
    """If `cwd` is inside a pm pool slot, return its (repo, uuid, path). Else None.

    Matches `<paths.worktrees>/<repo>/<uuid>/...` regardless of nesting depth.
    Resolves symlinks so this works when the user `cd`'d via a project
    forward symlink (`~/.projects/<name>/<repo>`).
    """
    target = (cwd or Path.cwd()).resolve()
    try:
        relative = target.relative_to(paths.worktrees.resolve())
    except ValueError:
        return None
    repo_and_uuid = relative.parts[:2]
    if len(repo_and_uuid) < len(("repo", "uuid")):
        return None
    repo, uuid = repo_and_uuid
    return CurrentSlot(repo_name=repo, uuid=uuid, path=paths.slot(repo, uuid))
