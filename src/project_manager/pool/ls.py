from dataclasses import dataclass

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OwnerKind, PoolDB


@dataclass(frozen=True)
class PoolRow:
    repo: str
    uuid: str
    status: str  # "FREE", a project name, or "OPS" for stacker-ops slots


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

    pooldb = PoolDB(paths.pool_db())
    rows: list[PoolRow] = []
    for r in repos:
        for s in slot_mod.list_slots(paths, r):
            owner = pooldb.get_owner(r, s.uuid)
            if owner is None:
                status = "FREE"
            elif owner.kind == OwnerKind.STACKER:
                status = "OPS"
            else:
                status = owner.id
            rows.append(PoolRow(repo=r, uuid=s.uuid, status=status))
    return rows
