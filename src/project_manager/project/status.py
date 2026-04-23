"""Single-project status: db rows joined with `check_project` findings."""

from dataclasses import dataclass

from project_manager import check as check_mod
from project_manager.paths import Paths
from project_manager.project import discovery


@dataclass(frozen=True)
class StatusRow:
    wt: str | None
    repo: str | None
    slot_uuid: str | None  # from db; None for findings without a wt row (orphan owner)
    finding: check_mod.Finding


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
