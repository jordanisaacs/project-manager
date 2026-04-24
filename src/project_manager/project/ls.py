from dataclasses import dataclass

from project_manager import check as check_mod
from project_manager.paths import Paths
from project_manager.project import discovery
from project_manager.render import Column, Section

_KIND_TO_LABEL: dict[check_mod.Kind, str] = {
    check_mod.Kind.ACTIVE: "attached",
    check_mod.Kind.DETACHED: "detached",
    check_mod.Kind.DRIFT: "drift",
    check_mod.Kind.STALE: "stale",
    check_mod.Kind.BROKEN: "broken",
    check_mod.Kind.OPS_OWNED: "ops-owned",
}

_STATUS_STYLE: dict[str, str] = {
    "attached": "green",
    "detached": "dim",
    "drift": "yellow",
    "stale": "red",
    "broken": "red",
    "ops-owned": "cyan",
}


@dataclass(frozen=True)
class ProjectRow:
    project: str
    wt: str
    repo: str
    slot_uuid: str
    status: str  # "attached" | "detached" | "drift" | "stale" | "broken"


COLUMNS: list[Column] = [
    Column("Worktree", "wt"),
    Column("Repo", "repo", style="blue"),
    Column("Slot", "slot_uuid", style="dim"),
    Column(
        "Status",
        "status",
        style=lambda r: _STATUS_STYLE.get(r.status, ""),
    ),
]


def sections(rows: list["ProjectRow"]) -> list[Section]:
    """Group project rows by project name for hierarchical display."""
    by_project: dict[str, list[ProjectRow]] = {}
    for row in rows:
        by_project.setdefault(row.project, []).append(row)
    return [Section(title=p, rows=rs) for p, rs in sorted(by_project.items())]


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
