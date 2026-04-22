import contextlib
from pathlib import Path

from project_manager.paths import Paths
from project_manager.project.errors import ProjectError
from project_manager.project.release import release


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


def delete(paths: Paths, project: str) -> None:
    """Auto-release + remove forward symlinks + rmdir.

    Errors if the project dir contains anything other than pm-managed symlinks.
    """
    project_dir = paths.project(project)
    if not project_dir.is_dir():
        raise ProjectError(f"project '{project}' does not exist")

    extras = [
        str(entry)
        for entry in project_dir.iterdir()
        if not _is_pm_symlink(entry, paths.worktrees)
    ]
    if extras:
        raise ProjectError(
            f"project '{project}' contains non-pm entries — remove them manually:\n  "
            + "\n  ".join(extras)
        )

    release(paths, project)

    for entry in list(project_dir.iterdir()):
        with contextlib.suppress(FileNotFoundError):
            entry.unlink()

    try:
        project_dir.rmdir()
    except OSError as e:
        raise ProjectError(f"could not rmdir {project_dir}: {e}") from e
