"""Single-project status: db rows joined with `check_project` findings."""

from dataclasses import dataclass

from project_manager import check as check_mod
from project_manager.paths import Paths
from project_manager.project import discovery
from project_manager.render import Column, Section

_KIND_STYLE: dict[check_mod.Kind, str] = {
    check_mod.Kind.ACTIVE: "green",
    check_mod.Kind.DETACHED: "dim",
    check_mod.Kind.DRIFT: "yellow",
    check_mod.Kind.STALE: "red",
    check_mod.Kind.BROKEN: "red",
    check_mod.Kind.ORPHAN_FORWARD: "red",
    check_mod.Kind.ORPHAN_OWNER: "red",
    check_mod.Kind.OPS_OWNED: "cyan",
}


@dataclass(frozen=True)
class StatusRow:
    wt: str | None
    repo: str | None
    slot_uuid: str | None  # from db; None for findings without a wt row (orphan owner)
    finding: check_mod.Finding

    def __pm_json__(self) -> dict:
        f = self.finding
        return {
            "wt": self.wt,
            "repo": self.repo,
            "slot_uuid": self.slot_uuid,
            "kind": f.kind.value,
            "forward_path": str(f.forward_path) if f.forward_path else None,
            "slot_path": str(f.slot_path) if f.slot_path else None,
            "detail": f.detail,
        }


COLUMNS: list[Column] = [
    Column(
        "Kind",
        lambda r: r.finding.kind.value,
        style=lambda r: _KIND_STYLE.get(r.finding.kind, ""),
    ),
    Column("Repo", lambda r: r.repo or "-", style="blue"),
    Column("Slot", lambda r: r.slot_uuid or "-", style="dim"),
    Column(
        "Path",
        lambda r: str(r.finding.forward_path or r.finding.slot_path or "-"),
    ),
    Column("Detail", lambda r: r.finding.detail),
]


def sections(rows: list["StatusRow"]) -> list[Section]:
    """Group findings by worktree; orphan-owner rows bucket into `<no wt>`."""
    by_wt: dict[str, list[StatusRow]] = {}
    for row in rows:
        by_wt.setdefault(row.wt or "<no wt>", []).append(row)
    return [Section(title=wt, rows=rs) for wt, rs in sorted(by_wt.items())]


def status(paths: Paths, project: str) -> list[StatusRow]:
    """Return one StatusRow per finding from `check_project(project)`.

    Raises ProjectError if the project doesn't exist.
    """
    wt_rows = {w: (r, u) for w, r, u in discovery.read_wts(paths, project)}
    findings = check_mod.check_project(paths, project)
    out: list[StatusRow] = []
    for f in findings:
        repo = f.repo
        slot_uuid: str | None = None
        if f.wt is not None and f.wt in wt_rows:
            row_repo, row_uuid = wt_rows[f.wt]
            slot_uuid = row_uuid
            repo = repo or row_repo
        out.append(
            StatusRow(wt=f.wt, repo=repo, slot_uuid=slot_uuid, finding=f)
        )
    return out
