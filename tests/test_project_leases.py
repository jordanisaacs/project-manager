import json
import sqlite3

import pytest

from project_manager import integration
from project_manager.cli import main
from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.project import add as add_mod
from project_manager.project import attach as attach_mod
from project_manager.project import detach as detach_mod
from project_manager.project import lease as lease_mod
from project_manager.project import remove as remove_mod
from tests.helpers import git_pool


def _project(paths: Paths) -> None:
    git_pool(paths, "repo", n=2)
    add_mod.add(paths, "demo", [("app", "repo")])


def test_acquire_snapshots_healthy_attached_worktrees(pm_env: Paths) -> None:
    _project(pm_env)

    acquired = lease_mod.acquire(pm_env, "demo", "test:one", "session-1")

    assert acquired.project == "demo"
    assert acquired.path == pm_env.project("demo")
    assert [(wt.name, wt.repo, wt.path) for wt in acquired.worktrees] == [
        ("app", "repo", pm_env.forward("demo", "app"))
    ]
    assert lease_mod.acquire(pm_env, "demo", "test:one", "session-1") == acquired


def test_release_is_idempotent(pm_env: Paths) -> None:
    _project(pm_env)
    lease_mod.acquire(pm_env, "demo", "test:one", "session-1")

    assert lease_mod.release(pm_env, "demo", "test:one", "session-1").released is True
    assert lease_mod.release(pm_env, "demo", "test:one", "session-1").released is False
    assert lease_mod.list_for_projects(pm_env, ["demo"]) == []


def test_pending_lease_finalizes_atomically(pm_env: Paths) -> None:
    _project(pm_env)

    pending = lease_mod.acquire_pending(pm_env, "demo", "https://omni.test", "pending-1")
    assert pending.state == "pending"
    assert pending.expires_at == pending.acquired_at + lease_mod.PENDING_TTL_SECONDS

    finalized = lease_mod.finalize(
        pm_env,
        "demo",
        "https://omni.test",
        "pending-1",
        "session-1",
    )
    assert finalized.state == "finalized"
    assert finalized.lease_id == "session-1"
    assert finalized.expires_at is None
    assert finalized.worktrees == pending.worktrees
    assert lease_mod.list_for_projects(pm_env, ["demo"]) == [finalized]


def test_expired_pending_lease_no_longer_blocks_topology(pm_env: Paths) -> None:
    _project(pm_env)
    pending = lease_mod.acquire_pending(
        pm_env,
        "demo",
        "https://omni.test",
        "pending-1",
        now=100,
    )
    assert pending.expires_at is not None
    with sqlite3.connect(pm_env.project_db("demo")) as conn:
        lease_mod.cleanup_expired(conn, now=pending.expires_at)

    assert len(detach_mod.detach(pm_env, "demo", ["app"])) == 1


def test_lease_keys_are_bounded_and_opaque(pm_env: Paths) -> None:
    _project(pm_env)

    lease = lease_mod.acquire(pm_env, "demo", "omnigent:install", "durable/session:id")
    assert lease.holder == "omnigent:install"
    assert lease.lease_id == "durable/session:id"
    with pytest.raises(ValueError, match="must not be empty"):
        lease_mod.acquire(pm_env, "demo", " ", "id")
    with pytest.raises(ValueError, match="at most 255"):
        lease_mod.acquire(pm_env, "demo", "x" * 256, "id")


def test_all_topology_mutations_are_blocked_by_a_lease(pm_env: Paths) -> None:
    _project(pm_env)
    lease_mod.acquire(pm_env, "demo", "omnigent:install", "session-1")

    with pytest.raises(ProjectError, match="omnigent:install:session-1"):
        add_mod.add(pm_env, "demo", [("other", "repo")])
    with pytest.raises(ProjectError, match="active lease"):
        detach_mod.detach(pm_env, "demo", ["app"])
    with pytest.raises(ProjectError, match="active lease"):
        remove_mod.remove(pm_env, "demo", ["app"])
    with pytest.raises(ProjectError, match="active lease"):
        remove_mod.remove(pm_env, "demo", None)


def test_attach_is_blocked_even_when_lease_snapshot_is_empty(pm_env: Paths) -> None:
    _project(pm_env)
    detach_mod.detach(pm_env, "demo", ["app"])
    acquired = lease_mod.acquire(pm_env, "demo", "test:one", "session-1")
    assert acquired.worktrees == ()

    with pytest.raises(ProjectError, match="active lease"):
        attach_mod.attach(pm_env, "demo", ["app"])


def test_last_release_unfreezes_topology(pm_env: Paths) -> None:
    _project(pm_env)
    lease_mod.acquire(pm_env, "demo", "test:one", "session-1")
    lease_mod.acquire(pm_env, "demo", "test:one", "session-2")
    lease_mod.release(pm_env, "demo", "test:one", "session-1")

    with pytest.raises(ProjectError, match="session-2"):
        detach_mod.detach(pm_env, "demo", ["app"])

    lease_mod.release(pm_env, "demo", "test:one", "session-2")
    assert len(detach_mod.detach(pm_env, "demo", ["app"])) == 1


def test_deleting_state_closes_acquire_race(pm_env: Paths) -> None:
    _project(pm_env)
    lease_mod.begin_delete(pm_env, "demo")

    with pytest.raises(ProjectError, match="being deleted"):
        lease_mod.acquire(pm_env, "demo", "test:one", "session-1")

    lease_mod.cancel_delete(pm_env, "demo")
    assert lease_mod.acquire(pm_env, "demo", "test:one", "session-1").lease_id == "session-1"


def test_list_filters_holder_across_projects(pm_env: Paths) -> None:
    add_mod.add(pm_env, "one", [])
    add_mod.add(pm_env, "two", [])
    lease_mod.acquire(pm_env, "one", "a", "1")
    lease_mod.acquire(pm_env, "two", "a", "2")
    lease_mod.acquire(pm_env, "two", "b", "3")

    rows = lease_mod.list_for_projects(pm_env, ["one", "two"], holder="a")
    assert [(row.project, row.lease_id) for row in rows] == [("one", "1"), ("two", "2")]


def test_cli_acquire_and_list_json(pm_env: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    add_mod.add(pm_env, "demo", [])

    assert (
        main(
            [
                "project",
                "lease",
                "acquire",
                "--holder",
                "test:cli",
                "--lease-id",
                "session-1",
                "--project",
                "demo",
                "--json",
            ]
        )
        == 0
    )
    acquired = json.loads(capsys.readouterr().out)
    assert acquired[0]["lease_id"] == "session-1"

    assert main(["project", "lease", "ls", "--all", "--holder", "test:cli", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(row["project"], row["lease_id"]) for row in listed] == [("demo", "session-1")]


def test_integration_projects_reports_live_and_excluded_worktrees(pm_env: Paths) -> None:
    _project(pm_env)
    add_mod.add(pm_env, "empty", [])
    detach_mod.detach(pm_env, "demo", ["app"])
    lease_mod.acquire(pm_env, "demo", "test:one", "session-1")

    projects = integration.projects(pm_env)

    assert [project.name for project in projects] == ["demo", "empty"]
    assert projects[0].lease_count == 1
    assert [(wt.name, wt.status) for wt in projects[0].worktrees] == [("app", "detached")]
    assert projects[1].worktrees == ()


def test_integration_json_envelope(pm_env: Paths) -> None:
    add_mod.add(pm_env, "demo", [])

    info = integration.info(pm_env).__pm_json__()
    assert info["protocol_version"] == 1
    assert info["projects_root"] == str(pm_env.projects)

    projects = [project.__pm_json__() for project in integration.projects(pm_env)]
    assert projects[0]["name"] == "demo"


def test_project_removal_clears_deleting_marker_on_failure(
    pm_env: Paths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _project(pm_env)

    def fail_detach(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(detach_mod, "detach", fail_detach)
    with pytest.raises(RuntimeError, match="boom"):
        remove_mod.remove(pm_env, "demo", None)

    assert lease_mod.acquire(pm_env, "demo", "test:one", "session-1").lease_id == "session-1"
