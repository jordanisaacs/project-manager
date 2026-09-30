"""Reconcile PM projects into Omnigent's first-class project API."""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from typing import Literal

from project_manager import config
from project_manager.config import Omnigent
from project_manager.paths import Paths
from project_manager.project import discovery

from .client import OmnigentApiError, Project, ProjectsClient
from .state import Mapping, SyncState

SyncAction = Literal["created", "linked", "updated", "unchanged", "deleted", "error"]
_HTTP_NOT_FOUND = 404
_HTTP_CONFLICT = 409


@dataclass(frozen=True)
class SyncResult:
    project: str
    action: SyncAction
    remote_id: str | None = None
    message: str | None = None

    @property
    def failed(self) -> bool:
        return self.action == "error"


@dataclass(frozen=True)
class _SyncContext:
    api: ProjectsClient
    state: SyncState
    paths: Paths
    settings: Omnigent
    remote_by_id: dict[str, Project]
    remote_by_name: dict[str, Project]


def sync_projects(
    paths: Paths,
    settings: Omnigent,
    *,
    client: ProjectsClient | None = None,
) -> list[SyncResult]:
    """Reconcile every local PM project and every tracked deletion."""
    state = SyncState(paths.omnigent_db())
    try:
        api = client or ProjectsClient(settings)
        remote_projects = api.list()
        mappings = {row.project_name: row for row in state.list(settings.server_url)}
    except (OmnigentApiError, OSError, sqlite3.Error) as error:
        return [SyncResult(project="*", action="error", message=str(error))]

    local_names = [name for name, _ in discovery.list_project_dbs(paths)]
    local_set = set(local_names)
    remote_by_id = {project.id: project for project in remote_projects}
    remote_by_name = {project.name: project for project in remote_projects}
    ctx = _SyncContext(
        api=api,
        state=state,
        paths=paths,
        settings=settings,
        remote_by_id=remote_by_id,
        remote_by_name=remote_by_name,
    )
    results: list[SyncResult] = []

    for name, mapping in mappings.items():
        if name in local_set:
            continue
        try:
            result = _delete_tracked(api, state, settings, mapping, remote_by_id)
        except (OSError, sqlite3.Error) as error:
            result = SyncResult(
                project=name,
                action="error",
                remote_id=mapping.remote_id,
                message=str(error),
            )
        results.append(result)
        if not result.failed:
            remote = remote_by_id.pop(mapping.remote_id, None)
            if remote is not None:
                remote_by_name.pop(remote.name, None)

    for name in local_names:
        mapping = mappings.get(name)
        try:
            result, project = _sync_one(ctx, name, mapping)
        except (OmnigentApiError, OSError, sqlite3.Error) as error:
            results.append(
                SyncResult(
                    project=name,
                    action="error",
                    remote_id=mapping.remote_id if mapping is not None else None,
                    message=str(error),
                )
            )
            continue
        results.append(result)
        remote_by_id[project.id] = project
        remote_by_name[project.name] = project
    return results


def best_effort_sync(paths: Paths) -> None:
    """Run automatic sync without rolling back a completed local operation."""
    try:
        settings = config.omnigent()
    except (ValueError, OSError) as error:
        _warn(str(error))
        return
    if settings is None:
        return
    try:
        results = sync_projects(paths, settings)
    except (OSError, sqlite3.Error) as error:
        _warn(str(error))
        return
    for result in results:
        if result.failed:
            detail = f": {result.message}" if result.message else ""
            _warn(f"could not sync {result.project!r}{detail}")


def _sync_one(
    ctx: _SyncContext,
    name: str,
    mapping: Mapping | None,
) -> tuple[SyncResult, Project]:
    current = ctx.remote_by_id.get(mapping.remote_id) if mapping is not None else None
    adopted = False
    if current is None:
        current = ctx.remote_by_name.get(name)
        adopted = current is not None

    managed_config = {
        "host_id": ctx.settings.host_id,
        "workspace": str(ctx.paths.project(name).resolve()),
    }
    if current is None:
        try:
            created = ctx.api.create(name, managed_config)
        except OmnigentApiError as error:
            if error.status_code != _HTTP_CONFLICT:
                raise
            created = _find_after_conflict(ctx.api, name)
            adopted = True
        current = created

    desired_config = {**current.config, **managed_config}
    needs_name = current.name != name
    needs_config = current.config != desired_config
    if needs_name or needs_config:
        previous_name = current.name
        current = ctx.api.update(
            current.id,
            name=name if needs_name else None,
            config=desired_config if needs_config else None,
        )
        if previous_name != current.name:
            cached = ctx.remote_by_name.get(previous_name)
            if cached is not None and cached.id == current.id:
                ctx.remote_by_name.pop(previous_name)
        action: SyncAction = "updated"
    elif mapping is None or mapping.remote_id != current.id:
        action = "linked" if adopted else "created"
    else:
        action = "unchanged"

    ctx.state.put(ctx.settings.server_url, name, current.id)
    ctx.remote_by_id[current.id] = current
    ctx.remote_by_name[current.name] = current
    return SyncResult(project=name, action=action, remote_id=current.id), current


def _delete_tracked(
    api: ProjectsClient,
    state: SyncState,
    settings: Omnigent,
    mapping: Mapping,
    remote_by_id: dict[str, Project],
) -> SyncResult:
    if mapping.remote_id in remote_by_id:
        try:
            api.delete(mapping.remote_id)
        except OmnigentApiError as error:
            if error.status_code != _HTTP_NOT_FOUND:
                return SyncResult(
                    project=mapping.project_name,
                    action="error",
                    remote_id=mapping.remote_id,
                    message=str(error),
                )
    state.remove(settings.server_url, mapping.project_name)
    return SyncResult(
        project=mapping.project_name,
        action="deleted",
        remote_id=mapping.remote_id,
    )


def _find_after_conflict(api: ProjectsClient, name: str) -> Project:
    for project in api.list():
        if project.name == name:
            return project
    raise OmnigentApiError(
        f"project {name!r} conflicted during creation but could not be found",
        status_code=409,
    )


def _warn(message: str) -> None:
    print(
        f"pm: warning: Omnigent sync {message}; retry with `pm omnigent sync`",
        file=sys.stderr,
    )
