import contextlib
from dataclasses import dataclass
from pathlib import Path

from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.project import db
from project_manager.project import detach as detach_mod

_DB_FILENAME = ".pm.db"
_README_FILENAME = "README.md"
_ALLOWED_EXTRAS = {_DB_FILENAME, _README_FILENAME}


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

    - `repos is None`: whole-project delete. Project-level extras check, then
      per-repo delete for every repo, then remove README + drop db + rmdir.
    - `repos is not None`: per-repo delete. Detach (inherits cleanliness
      protections), then drop the db row. Saved branch is lost.
    """
    project_dir = paths.project(project)
    db_path = paths.project_db(project)
    if not db_path.is_file():
        raise ProjectError(f"project '{project}' does not exist")

    if repos is None:
        _delete_whole(paths, project, project_dir, db_path)
    else:
        _delete_repos(paths, project, repos, db_path)


def _delete_repos(
    paths: Paths, project: str, repos: list[str], db_path: Path
) -> None:
    """Per-repo delete helper. Detach each repo (cleanliness-checked), drop rows."""
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

    with db.transaction(db_path) as conn:
        all_repos = [name for name, _ in db.list_repos(conn)]
    if all_repos:
        _delete_repos(paths, project, all_repos, db_path)

    with contextlib.suppress(FileNotFoundError):
        (project_dir / _README_FILENAME).unlink()
    with contextlib.suppress(FileNotFoundError):
        db_path.unlink()
    try:
        project_dir.rmdir()
    except OSError as e:
        raise ProjectError(f"could not rmdir {project_dir}: {e}") from e


@dataclass(frozen=True)
class DeletePlan:
    project: str
    whole: bool
    extras: list[Path]
    detach_plan: detach_mod.DetachPlan
    drop_rows: list[str]
    remove_readme: bool
    drop_db: bool
    rmdir: bool

    @property
    def has_blocker(self) -> bool:
        return bool(self.extras) or self.detach_plan.has_blocker


def plan_delete(
    paths: Paths, project: str, repos: list[str] | None
) -> DeletePlan:
    """Describe what `delete` would do without mutating state."""
    project_dir = paths.project(project)
    db_path = paths.project_db(project)
    if not db_path.is_file():
        raise ProjectError(f"project '{project}' does not exist")

    whole = repos is None
    extras: list[Path] = []
    if whole:
        extras = [
            entry
            for entry in project_dir.iterdir()
            if entry.name not in _ALLOWED_EXTRAS
            and not _is_pm_symlink(entry, paths.worktrees)
        ]
        with db.readonly(db_path) as conn:
            planned_repos = [name for name, _ in db.list_repos(conn)]
    else:
        assert repos is not None
        planned_repos = list(repos)

    detach_plan = detach_mod.plan_detach(paths, project, planned_repos)
    return DeletePlan(
        project=project,
        whole=whole,
        extras=extras,
        detach_plan=detach_plan,
        drop_rows=planned_repos,
        remove_readme=whole,
        drop_db=whole,
        rmdir=whole,
    )
