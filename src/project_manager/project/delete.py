import contextlib
from pathlib import Path

from project_manager.paths import Paths
from project_manager.project import db
from project_manager.project import detach as detach_mod
from project_manager.project.errors import ProjectError

_DB_FILENAME = ".pm.db"
_ALLOWED_EXTRAS = {_DB_FILENAME, "README.md"}


def _is_pm_symlink(entry: Path, worktrees_root: Path) -> bool:
    if not entry.is_symlink():
        return False
    target = entry.readlink()
    if not target.is_absolute():
        return False
    try:
        target.relative_to(worktrees_root)
    except ValueError:
        return False
    return True


def delete(paths: Paths, project: str, repos: list[str] | None) -> None:
    """Delete repo(s) from a project.

    - `repos is None`: whole-project delete. Detach everything, drop the db, rmdir.
      Safety check rejects non-pm entries in the project dir.
    - `repos is not None`: per-repo delete. Implicit detach if attached, drop row.
    """
    project_dir = paths.project(project)
    db_path = paths.project_db(project)
    if not db_path.is_file():
        raise ProjectError(f"project '{project}' does not exist")

    if repos is None:
        _delete_whole(paths, project, project_dir, db_path)
    else:
        _delete_per_repo(paths, project, repos, db_path)


def _delete_whole(paths: Paths, project: str, project_dir: Path, db_path: Path) -> None:
    extras = [
        str(entry)
        for entry in project_dir.iterdir()
        if entry.name not in _ALLOWED_EXTRAS
        and not _is_pm_symlink(entry, paths.worktrees)
    ]
    if extras:
        raise ProjectError(
            f"project '{project}' contains non-pm entries — remove them manually:\n  "
            + "\n  ".join(extras)
        )

    detach_mod.detach(paths, project, repos=None)

    for entry in list(project_dir.iterdir()):
        if entry.name == _DB_FILENAME:
            continue
        with contextlib.suppress(FileNotFoundError):
            entry.unlink()
    with contextlib.suppress(FileNotFoundError):
        db_path.unlink()
    try:
        project_dir.rmdir()
    except OSError as e:
        raise ProjectError(f"could not rmdir {project_dir}: {e}") from e


def _delete_per_repo(
    paths: Paths, project: str, repos: list[str], db_path: Path
) -> None:
    with db.transaction(db_path) as conn:
        known = {name for name, _ in db.list_repos(conn)}
        missing = [r for r in repos if r not in known]
        if missing:
            raise ProjectError(
                f"project '{project}' has no such repo(s): {', '.join(missing)}"
            )
        detach_mod.detach(paths, project, repos=repos)
        for r in repos:
            db.remove_repo(conn, r)
