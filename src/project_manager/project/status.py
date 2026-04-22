"""Single-project status: db rows joined with `check_project` findings."""

from dataclasses import dataclass

from project_manager import check as check_mod
from project_manager.paths import Paths
from project_manager.project import discovery


@dataclass(frozen=True)
class StatusRow:
    repo: str | None
    slot_uuid: str | None  # from db; None for findings without a repo row (orphan owner)
    finding: check_mod.Finding


def status(paths: Paths, project: str) -> list[StatusRow]:
    """Return one StatusRow per finding from `check_project(project)`.

    Raises ProjectError if the project doesn't exist.
    """
    db_rows = dict(discovery.read_repos(paths, project))
    findings = check_mod.check_project(paths, project)
    return [
        StatusRow(
            repo=f.repo,
            slot_uuid=db_rows.get(f.repo) if f.repo is not None else None,
            finding=f,
        )
        for f in findings
    ]
