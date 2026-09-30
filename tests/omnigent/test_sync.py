from __future__ import annotations

import builtins
from typing import Any

from project_manager.config import Omnigent
from project_manager.omnigent.client import OmnigentApiError, Project, ProjectsClient
from project_manager.omnigent.state import SyncState
from project_manager.omnigent.sync import sync_projects
from project_manager.paths import Paths
from project_manager.project import add as add_mod
from project_manager.project import remove as remove_mod


class FakeProjectsClient(ProjectsClient):
    def __init__(self, projects: builtins.list[Project] | None = None) -> None:
        self.projects = {project.id: project for project in projects or []}
        self.next_id = 1
        self.creates = 0
        self.updates = 0
        self.deletes = 0
        self.fail_delete = False

    def list(self) -> builtins.list[Project]:
        return list(self.projects.values())

    def create(self, name: str, config: dict[str, Any]) -> Project:
        if any(project.name == name for project in self.projects.values()):
            raise OmnigentApiError("duplicate", status_code=409)
        project = Project(id=f"new-{self.next_id}", name=name, config=dict(config))
        self.next_id += 1
        self.creates += 1
        self.projects[project.id] = project
        return project

    def update(
        self,
        project_id: str,
        *,
        name: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> Project:
        current = self.projects[project_id]
        project = Project(
            id=current.id,
            name=name if name is not None else current.name,
            config=dict(config) if config is not None else current.config,
        )
        self.updates += 1
        self.projects[project_id] = project
        return project

    def delete(self, project_id: str) -> None:
        if self.fail_delete:
            raise OmnigentApiError("temporarily unavailable", status_code=503)
        self.deletes += 1
        self.projects.pop(project_id, None)


def _settings(*, host_id: str = "host_1") -> Omnigent:
    return Omnigent(server_url="http://omnigent.test", host_id=host_id)


def test_backfill_adopts_by_name_preserves_other_defaults_and_is_idempotent(
    pm_env: Paths,
) -> None:
    add_mod.add(pm_env, "Alpha", [])
    add_mod.add(pm_env, "Beta", [])
    client = FakeProjectsClient(
        [
            Project(
                id="existing",
                name="Alpha",
                config={"host_id": "old", "workspace": "/old", "agent_id": "agent_1"},
            ),
            Project(id="unrelated", name="UI only", config={}),
        ]
    )

    first = sync_projects(pm_env, _settings(), client=client)

    assert [(row.project, row.action) for row in first] == [
        ("Alpha", "updated"),
        ("Beta", "created"),
    ]
    assert client.projects["existing"].config == {
        "host_id": "host_1",
        "workspace": str(pm_env.project("Alpha")),
        "agent_id": "agent_1",
    }
    assert client.projects["unrelated"].name == "UI only"
    assert {
        row.project_name: row.remote_id
        for row in SyncState(pm_env.omnigent_db()).list(_settings().server_url)
    } == {"Alpha": "existing", "Beta": "new-1"}

    mutations = (client.creates, client.updates, client.deletes)
    second = sync_projects(pm_env, _settings(), client=client)
    assert [row.action for row in second] == ["unchanged", "unchanged"]
    assert (client.creates, client.updates, client.deletes) == mutations


def test_remote_rename_and_host_change_are_reconciled_by_id(pm_env: Paths) -> None:
    add_mod.add(pm_env, "Alpha", [])
    client = FakeProjectsClient()
    sync_projects(pm_env, _settings(), client=client)
    remote_id = next(iter(client.projects))
    client.projects[remote_id] = Project(
        id=remote_id,
        name="Renamed in Omnigent",
        config={"host_id": "host_1", "workspace": "/old", "model": "keep-me"},
    )

    rows = sync_projects(pm_env, _settings(host_id="host_2"), client=client)

    assert rows[0].action == "updated"
    assert client.projects[remote_id] == Project(
        id=remote_id,
        name="Alpha",
        config={
            "host_id": "host_2",
            "workspace": str(pm_env.project("Alpha")),
            "model": "keep-me",
        },
    )


def test_failed_delete_keeps_tombstone_for_retry(pm_env: Paths) -> None:
    add_mod.add(pm_env, "Doomed", [])
    client = FakeProjectsClient()
    sync_projects(pm_env, _settings(), client=client)
    remote_id = next(iter(client.projects))
    remove_mod.remove(pm_env, "Doomed", None)
    client.fail_delete = True

    failed = sync_projects(pm_env, _settings(), client=client)

    assert failed[0].action == "error"
    assert client.projects[remote_id].name == "Doomed"
    assert SyncState(pm_env.omnigent_db()).list(_settings().server_url)

    client.fail_delete = False
    retried = sync_projects(pm_env, _settings(), client=client)
    assert retried[0].action == "deleted"
    assert remote_id not in client.projects
    assert SyncState(pm_env.omnigent_db()).list(_settings().server_url) == []


def test_remote_deletion_is_recreated_and_mapping_is_replaced(pm_env: Paths) -> None:
    add_mod.add(pm_env, "Alpha", [])
    client = FakeProjectsClient()
    sync_projects(pm_env, _settings(), client=client)
    old_id = next(iter(client.projects))
    client.projects.clear()

    rows = sync_projects(pm_env, _settings(), client=client)

    assert rows[0].action == "created"
    new_id = next(iter(client.projects))
    assert new_id != old_id
    assert SyncState(pm_env.omnigent_db()).list(_settings().server_url)[0].remote_id == new_id


def test_untracked_omnigent_project_is_never_pruned(pm_env: Paths) -> None:
    client = FakeProjectsClient([Project(id="manual", name="Manual", config={})])
    assert sync_projects(pm_env, _settings(), client=client) == []
    assert client.projects["manual"].name == "Manual"
