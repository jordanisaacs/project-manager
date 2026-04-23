"""Shared helpers for discovering projects and reading their db state."""

from pathlib import Path

from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.project import db


def list_project_dbs(paths: Paths) -> list[tuple[str, Path]]:
    """Return [(project_name, db_path), ...] for every project dir with a .pm.db."""
    if not paths.projects.is_dir():
        return []
    out: list[tuple[str, Path]] = []
    for project_dir in sorted(paths.projects.iterdir()):
        if not project_dir.is_dir():
            continue
        db_path = paths.project_db(project_dir.name)
        if db_path.is_file():
            out.append((project_dir.name, db_path))
    return out


def require_project_db(paths: Paths, project: str) -> Path:
    """Return the .pm.db path for a project, or raise ProjectError if missing."""
    db_path = paths.project_db(project)
    if not db_path.is_file():
        raise ProjectError(f"project '{project}' does not exist")
    return db_path


def read_repos(paths: Paths, project: str) -> list[tuple[str, str]]:
    """Return the project's (repo, slot_uuid) rows. Raises ProjectError if missing."""
    db_path = require_project_db(paths, project)
    with db.readonly(db_path) as conn:
        return db.list_repos(conn)
