"""`pm project lease` commands."""

from typing import Annotated

from cyclopts import App, Parameter

from project_manager import config, render
from project_manager.cli._shared import (
    ProjectFlag,
    ProjectOrAllScope,
    resolve_project_scope,
)
from project_manager.project import current
from project_manager.project import lease as lease_mod
from project_manager.project.cli import project_app

lease_app = project_app.command(
    App(name="lease", help="manage durable project topology leases"),
)

LEASE_COLUMNS = [
    render.Column("Project", "project", style="bold"),
    render.Column("Holder", "holder"),
    render.Column("Lease", "lease_id"),
    render.Column("Acquired", "acquired_at", style="dim"),
    render.Column("Worktrees", lambda row: len(row.worktrees)),
]

RELEASE_COLUMNS = [
    render.Column("Project", "project", style="bold"),
    render.Column("Holder", "holder"),
    render.Column("Lease", "lease_id"),
    render.Column("Released", "released"),
]


@lease_app.command
def acquire(
    holder: Annotated[str, Parameter(name="--holder")],
    lease_id: Annotated[str, Parameter(name="--lease-id")],
    flag: ProjectFlag = ProjectFlag(),
    *,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Acquire a durable lease on a project's attached-worktree topology."""
    paths = config.load()
    project = current.resolve_project(paths, flag.project)
    result = lease_mod.acquire(paths, project, holder, lease_id)
    render.emit_rows([result], LEASE_COLUMNS, as_json=json)
    return 0


@lease_app.command
def release(
    holder: Annotated[str, Parameter(name="--holder")],
    lease_id: Annotated[str, Parameter(name="--lease-id")],
    flag: ProjectFlag = ProjectFlag(),
    *,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Idempotently release one project lease."""
    paths = config.load()
    project = current.resolve_project(paths, flag.project)
    result = lease_mod.release(paths, project, holder, lease_id)
    render.emit_rows([result], RELEASE_COLUMNS, as_json=json)
    return 0


@lease_app.command(name="ls")
def list_leases(
    scope: ProjectOrAllScope = ProjectOrAllScope(),
    *,
    holder: Annotated[str | None, Parameter(name="--holder")] = None,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """List project leases in one project or across all projects."""
    paths = config.load()
    projects = resolve_project_scope(paths, scope)
    rows = lease_mod.list_for_projects(paths, projects, holder=holder)
    render.emit_rows(rows, LEASE_COLUMNS, as_json=json)
    return 0
