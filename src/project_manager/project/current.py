"""Detect the current project from cwd, so CLI handlers can default the name arg."""

import contextlib
import os
from pathlib import Path

from project_manager.errors import ProjectError
from project_manager.paths import Paths


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
    """If `path` is under `paths.worktrees`, read `<repo>/<uuid>/.owner` to find project."""
    try:
        rel = path.relative_to(paths.worktrees)
    except ValueError:
        return None
    if len(rel.parts) < 2:  # noqa: PLR2004
        return None
    repo, uuid = rel.parts[0], rel.parts[1]
    owner = paths.owner(repo, uuid)
    if not owner.is_symlink():
        return None
    target = owner.readlink()
    try:
        target_rel = target.relative_to(paths.projects)
    except ValueError:
        return None
    if not target_rel.parts:
        return None
    return target_rel.parts[0]


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
