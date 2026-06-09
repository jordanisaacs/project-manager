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


def remove(paths: Paths, project: str, wts: list[str] | None) -> None:
    """Remove worktree(s) from a project.

    - `wts is None`: whole-project removal. Project-level extras check, then
      per-wt removal for every wt, then remove README + drop db + rmdir.
    - `wts is not None`: per-wt removal. Detach (inherits cleanliness
      protections), then drop the db row. Saved branch is lost.
    """
    project_dir = paths.project(project)
    db_path = paths.project_db(project)
    if not db_path.is_file():
        raise ProjectError(f"project '{project}' does not exist")

    if wts is None:
        _remove_whole(paths, project, project_dir, db_path)
    else:
        _remove_wts(paths, project, wts, db_path)


def _remove_wts(
    paths: Paths,
    project: str,
    wts: list[str],
    db_path: Path,
) -> None:
    """Per-wt removal helper. Detach each wt (cleanliness-checked), drop rows."""
    with db.transaction(db_path) as conn:
        known = {name for name, _, _ in db.list_wts(conn)}
        missing = [w for w in wts if w not in known]
        if missing:
            raise ProjectError(f"project '{project}' has no such worktree(s): {', '.join(missing)}")
        detach_mod.detach(paths, project, wts=wts)
        for w in wts:
            db.remove_wt(conn, w)


def _remove_whole(paths: Paths, project: str, project_dir: Path, db_path: Path) -> None:
    extras = [
        str(entry)
        for entry in project_dir.iterdir()
        if entry.name not in _ALLOWED_EXTRAS and not _is_pm_symlink(entry, paths.worktrees)
    ]
    if extras:
        raise ProjectError(
            f"project '{project}' contains non-pm entries — remove them manually:\n  "
            + "\n  ".join(extras)
        )

    with db.transaction(db_path) as conn:
        all_wts = [name for name, _, _ in db.list_wts(conn)]
    if all_wts:
        _remove_wts(paths, project, all_wts, db_path)

    with contextlib.suppress(FileNotFoundError):
        (project_dir / _README_FILENAME).unlink()
    with contextlib.suppress(FileNotFoundError):
        db_path.unlink()
    try:
        project_dir.rmdir()
    except OSError as e:
        raise ProjectError(f"could not rmdir {project_dir}: {e}") from e


@dataclass(frozen=True)
class RemovePlan:
    project: str
    whole: bool
    extras: list[Path]
    detach_plan: detach_mod.DetachPlan
    drop_rows: list[str]  # wt names
    remove_readme: bool
    drop_db: bool
    rmdir: bool

    @property
    def has_blocker(self) -> bool:
        return bool(self.extras) or self.detach_plan.has_blocker

    def __pm_json__(self) -> dict:
        return {
            "project": self.project,
            "whole": self.whole,
            "has_blocker": self.has_blocker,
            "extras": [str(e) for e in self.extras],
            "detach": self.detach_plan.__pm_json__(),
            "drop_rows": list(self.drop_rows),
            "remove_readme": self.remove_readme,
            "drop_db": self.drop_db,
            "rmdir": self.rmdir,
        }


def plan_remove(
    paths: Paths,
    project: str,
    wts: list[str] | None,
) -> RemovePlan:
    """Describe what `remove` would do without mutating state."""
    project_dir = paths.project(project)
    db_path = paths.project_db(project)
    if not db_path.is_file():
        raise ProjectError(f"project '{project}' does not exist")

    whole = wts is None
    extras: list[Path] = []
    if whole:
        extras = [
            entry
            for entry in project_dir.iterdir()
            if entry.name not in _ALLOWED_EXTRAS and not _is_pm_symlink(entry, paths.worktrees)
        ]
        with db.readonly(db_path) as conn:
            planned_wts = [name for name, _, _ in db.list_wts(conn)]
    else:
        assert wts is not None
        planned_wts = list(wts)

    detach_plan = detach_mod.plan_detach(paths, project, planned_wts)
    return RemovePlan(
        project=project,
        whole=whole,
        extras=extras,
        detach_plan=detach_plan,
        drop_rows=planned_wts,
        remove_readme=whole,
        drop_db=whole,
        rmdir=whole,
    )
