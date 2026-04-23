"""Detect the current project from cwd, so CLI handlers can default the name arg."""

import contextlib
import os
from pathlib import Path

from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.pool.db import OwnerKind, PoolDB


def _cwd_candidates() -> list[Path]:
    """Logical ($PWD) and physical (os.getcwd) cwd paths, de-duped."""
    out: list[Path] = []
    if pwd := os.environ.get("PWD"):
        out.append(Path(pwd))
    with contextlib.suppress(FileNotFoundError):
        out.append(Path.cwd())
    seen: set[Path] = set()
    result: list[Path] = []
    for p in out:
        if p in seen:
            continue
        seen.add(p)
        result.append(p)
    return result


def project_from_projects_path(path: Path, paths: Paths) -> str | None:
    """If `path` is under `paths.projects`, return the next component as project name."""
    try:
        rel = path.relative_to(paths.projects)
    except ValueError:
        return None
    if not rel.parts:
        return None
    return rel.parts[0]


def project_from_worktree_path(path: Path, paths: Paths) -> str | None:
    """If `path` is under `paths.worktrees`, look up the owning project in pool db."""
    try:
        rel = path.relative_to(paths.worktrees)
    except ValueError:
        return None
    if len(rel.parts) < 2:  # noqa: PLR2004
        return None
    repo, uuid = rel.parts[0], rel.parts[1]
    owner = PoolDB(paths.pool_db()).get_owner(repo, uuid)
    if owner is None or owner.kind != OwnerKind.PROJECT:
        return None
    return owner.id


def detect_current_project(paths: Paths) -> str | None:
    """Return the name of the project the user's cwd is inside, or None."""
    for cwd in _cwd_candidates():
        if p := project_from_projects_path(cwd, paths):
            return p
        if p := project_from_worktree_path(cwd, paths):
            return p
    return None


def resolve_project(paths: Paths, explicit: str | None) -> str:
    """Return `explicit` if given, else the detected current project, else raise."""
    if explicit:
        return explicit
    detected = detect_current_project(paths)
    if detected is None:
        raise ProjectError("no project specified and cwd is not inside a project")
    return detected
