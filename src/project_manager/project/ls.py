from dataclasses import dataclass

from project_manager.paths import Paths
from project_manager.pool.slot import Slot
from project_manager.project import db


@dataclass(frozen=True)
class ProjectRow:
    project: str
    repo: str
    slot_uuid: str
    status: str  # "attached" | "detached" | "drift" | "stale" | "broken"


def _status(paths: Paths, project: str, repo: str, slot_uuid: str) -> tuple[str, str]:
    """Return (status, displayed_uuid). displayed_uuid reflects reality (the forward target)."""
    forward = paths.forward(project, repo)
    if not forward.is_symlink():
        return "detached", slot_uuid

    target = forward.readlink()
    actual_uuid = target.name
    if not target.is_dir():
        return "broken", actual_uuid

    s = Slot(repo=repo, uuid=actual_uuid, path=target)
    owner = s.owner_target()
    if owner is None:
        return "stale", actual_uuid
    if owner != forward:
        return "stale", actual_uuid
    if actual_uuid != slot_uuid:
        return "drift", actual_uuid
    return "attached", actual_uuid


def ls(paths: Paths) -> list[ProjectRow]:
    if not paths.projects.is_dir():
        return []
    rows: list[ProjectRow] = []
    for project_dir in sorted(paths.projects.iterdir()):
        if not project_dir.is_dir():
            continue
        db_path = paths.project_db(project_dir.name)
        if not db_path.is_file():
            continue
        with db.readonly(db_path) as conn:
            for name, slot_uuid in db.list_repos(conn):
                status, displayed = _status(paths, project_dir.name, name, slot_uuid)
                rows.append(
                    ProjectRow(
                        project=project_dir.name,
                        repo=name,
                        slot_uuid=displayed,
                        status=status,
                    )
                )
    return rows
