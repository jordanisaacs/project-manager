"""Stable project read model exposed by the project-manager daemon."""

from dataclasses import dataclass
from pathlib import Path

from project_manager.paths import Paths
from project_manager.project import discovery
from project_manager.project import lease as lease_mod
from project_manager.project import status as status_mod

PROTOCOL_VERSION = 1


@dataclass(frozen=True)
class IntegrationInfo:
    protocol_version: int
    projects_root: Path

    def __pm_json__(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "projects_root": str(self.projects_root),
        }


@dataclass(frozen=True)
class IntegrationWorktree:
    name: str
    repo: str
    status: str
    path: Path
    slot_uuid: str | None
    branch: str | None
    detail: str

    @property
    def included(self) -> bool:
        return self.status == "active"

    def __pm_json__(self) -> dict[str, object]:
        return {
            "name": self.name,
            "repo": self.repo,
            "status": self.status,
            "path": str(self.path),
            "slot_uuid": self.slot_uuid,
            "branch": self.branch,
            "detail": self.detail,
            "included": self.included,
        }


@dataclass(frozen=True)
class IntegrationProject:
    name: str
    path: Path
    lease_count: int
    worktrees: tuple[IntegrationWorktree, ...]

    def __pm_json__(self) -> dict[str, object]:
        return {
            "id": self.name,
            "object": "pm.project",
            "name": self.name,
            "path": str(self.path),
            "workspace": str(self.path),
            "lease_count": self.lease_count,
            "worktrees": [worktree.__pm_json__() for worktree in self.worktrees],
            "directories": [
                {"name": worktree.name, "path": str(worktree.path)}
                for worktree in self.worktrees
                if worktree.included
            ],
        }


def info(paths: Paths) -> IntegrationInfo:
    """Return protocol and canonical project-root information."""
    return IntegrationInfo(PROTOCOL_VERSION, paths.projects.resolve())


def projects(paths: Paths) -> list[IntegrationProject]:
    """Return every project with live health and lease information."""
    out: list[IntegrationProject] = []
    for name, _db_path in discovery.list_project_dbs(paths):
        project_status = status_mod.status(
            paths,
            name,
            frozenset({status_mod.StatusSection.WORKTREES}),
        )
        worktrees = tuple(
            IntegrationWorktree(
                name=row.wt,
                repo=row.repo,
                status=row.finding.kind.value,
                path=paths.forward(name, row.wt),
                slot_uuid=row.finding.slot_path.name if row.finding.slot_path else None,
                branch=row.branch,
                detail=row.finding.detail,
            )
            for row in project_status.worktrees
            if row.wt is not None and row.repo is not None
        )
        leases = lease_mod.list_for_projects(paths, [name])
        out.append(
            IntegrationProject(
                name=name,
                path=paths.project(name).resolve(),
                lease_count=len(leases),
                worktrees=worktrees,
            )
        )
    return out
