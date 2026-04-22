from dataclasses import dataclass

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod


@dataclass(frozen=True)
class PoolRow:
    repo: str
    uuid: str
    status: str  # "FREE" or project name


def ls(paths: Paths, repo: str | None) -> list[PoolRow]:
    repos: list[str]
    if repo is not None:
        repos = [repo]
    else:
        if not paths.worktrees.is_dir():
            return []
        repos = sorted(
            entry.name for entry in paths.worktrees.iterdir() if entry.is_dir()
        )

    rows: list[PoolRow] = []
    for r in repos:
        for s in slot_mod.list_slots(paths, r):
            owner = s.owner_target()
            # owner points at <projects>/<project>/<repo>; parent.name is the project
            status = "FREE" if owner is None else owner.parent.name
            rows.append(PoolRow(repo=r, uuid=s.uuid, status=status))
    return rows
