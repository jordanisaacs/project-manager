from dataclasses import dataclass

from project_manager import check as check_mod
from project_manager.paths import Paths
from project_manager.project import discovery

_KIND_TO_LABEL: dict[check_mod.Kind, str] = {
    check_mod.Kind.ACTIVE: "attached",
    check_mod.Kind.DETACHED: "detached",
    check_mod.Kind.DRIFT: "drift",
    check_mod.Kind.STALE: "stale",
    check_mod.Kind.BROKEN: "broken",
    check_mod.Kind.OPS_OWNED: "ops-owned",
}


@dataclass(frozen=True)
class ProjectRow:
    project: str
    wt: str
    repo: str
    slot_uuid: str
    status: str  # "attached" | "detached" | "drift" | "stale" | "broken"


def ls(paths: Paths) -> list[ProjectRow]:
    rows: list[ProjectRow] = []
    for project, _ in discovery.list_project_dbs(paths):
        findings = check_mod.check_project(paths, project, include_orphan_owners=False)
        for f in findings:
            label = _KIND_TO_LABEL.get(f.kind)
            if (
                label is None
                or f.wt is None
                or f.repo is None
                or f.slot_path is None
            ):
                continue
            rows.append(
                ProjectRow(
                    project=project,
                    wt=f.wt,
                    repo=f.repo,
                    slot_uuid=f.slot_path.name,
                    status=label,
                ),
            )
    return rows
