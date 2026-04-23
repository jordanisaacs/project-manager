from dataclasses import dataclass

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod


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

    ops_marker = paths.stacker_ops_marker()
    rows: list[PoolRow] = []
    for r in repos:
        for s in slot_mod.list_slots(paths, r):
            owner = s.owner_target()
            if owner is None:
                status = "FREE"
            elif _is_ops_marker(owner, ops_marker):
                status = "OPS"
            else:
                # owner points at <projects>/<project>/<repo>; parent.name is the project
                status = owner.parent.name
            rows.append(PoolRow(repo=r, uuid=s.uuid, status=status))
    return rows


def _is_ops_marker(owner, ops_marker) -> bool:  # noqa: ANN001
    try:
        return owner.resolve() == ops_marker.resolve()
    except OSError:
        return False
