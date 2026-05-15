from dataclasses import dataclass

from project_manager import check as check_mod
from project_manager.paths import Paths
from project_manager.project import branch as branch_mod
from project_manager.project import db, discovery
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

_DETACHED_HEAD = "(detached)"
_NO_BRANCH = "-"


@dataclass(frozen=True)
class ProjectRow:
    project: str
    wt: str
    repo: str
    branch: str
    status: str  # "attached" | "detached" | "drift" | "stale" | "broken"


def _branch_style(row: "ProjectRow") -> str:
    if row.branch == _DETACHED_HEAD:
        return "italic"
    if row.status == "detached":
        return "dim"
    return ""


COLUMNS: list[Column] = [
    Column("Worktree", "wt"),
    Column("Repo", "repo", style="blue"),
    Column("Branch", "branch", style=_branch_style),
    Column(
        "Status",
        "status",
        style=lambda r: _STATUS_STYLE.get(r.status, ""),
    ),
]


def sections(
    rows: list["ProjectRow"],
    projects: list[str] | None = None,
) -> list[Section]:
    """Group project rows by project name for hierarchical display.

    PROJECTS, when given, seeds the grouping with every known project so
    that ones with zero attached worktrees still appear as empty sections
    instead of being dropped.
    """
    by_project: dict[str, list[ProjectRow]] = {}
    if projects is not None:
        for p in projects:
            by_project[p] = []
    for row in rows:
        by_project.setdefault(row.project, []).append(row)
    return [Section(title=p, rows=rs) for p, rs in sorted(by_project.items())]


def _branch_for(paths: Paths, project: str, finding: check_mod.Finding) -> str:
    """Resolve the branch string for a project-ls row.

    Attached/drift rows read the live slot's HEAD. Detached rows fall
    back to the saved branch stored in the project db (populated on
    detach for re-attach restore).
    """
    if finding.kind in (check_mod.Kind.ACTIVE, check_mod.Kind.DRIFT):
        if finding.slot_path is not None:
            current = branch_mod.read_current_branch(finding.slot_path)
            if current is not None:
                return current
        return _DETACHED_HEAD
    if finding.kind == check_mod.Kind.DETACHED and finding.wt is not None:
        db_path = discovery.require_project_db(paths, project)
        with db.readonly(db_path) as conn:
            saved = db.get_branch(conn, finding.wt)
        return saved or _NO_BRANCH
    return _NO_BRANCH


def ls(paths: Paths) -> list[ProjectRow]:
    rows: list[ProjectRow] = []
    for project, _ in discovery.list_project_dbs(paths):
        findings = check_mod.check_project(paths, project, include_orphan_owners=False)
        for f in findings:
            label = _KIND_TO_LABEL.get(f.kind)
            if label is None or f.wt is None or f.repo is None or f.slot_path is None:
                continue
            rows.append(
                ProjectRow(
                    project=project,
                    wt=f.wt,
                    repo=f.repo,
                    branch=_branch_for(paths, project, f),
                    status=label,
                ),
            )
    return rows
