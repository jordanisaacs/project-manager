from dataclasses import dataclass
from pathlib import Path

from project_manager.paths import Paths
from project_manager.pool.slot import Slot


@dataclass(frozen=True)
class ProjectRow:
    project: str
    repo: str
    uuid: str
    status: str  # "active" | "detached" | "stale" | "broken"


def _status(entry: Path, target: Path, repo: str, uuid: str) -> str:
    if not target.is_dir():
        return "broken"
    s = Slot(repo=repo, uuid=uuid, path=target)
    owner = s.owner_target()
    if owner is None:
        return "detached"
    if owner == entry:
        return "active"
    return "stale"


def ls(paths: Paths) -> list[ProjectRow]:
    if not paths.projects.is_dir():
        return []
    rows: list[ProjectRow] = []
    for project_dir in sorted(paths.projects.iterdir()):
        if not project_dir.is_dir():
            continue
        for entry in sorted(project_dir.iterdir()):
            if not entry.is_symlink():
                continue
            target = entry.readlink()
            rows.append(
                ProjectRow(
                    project=project_dir.name,
                    repo=entry.name,
                    uuid=target.name,
                    status=_status(entry, target, entry.name, target.name),
                )
            )
    return rows
