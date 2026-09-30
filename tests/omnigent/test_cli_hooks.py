from __future__ import annotations

import importlib

from project_manager.paths import Paths

pm_cli = importlib.import_module("project_manager.cli")
create_cli = importlib.import_module("project_manager.project.cli.create")
delete_cli = importlib.import_module("project_manager.project.cli.delete")


def test_project_create_and_delete_trigger_best_effort_sync(
    pm_env: Paths,
    monkeypatch,
) -> None:
    calls: list[Paths] = []
    monkeypatch.setattr(create_cli, "best_effort_sync", calls.append)
    monkeypatch.setattr(delete_cli, "best_effort_sync", calls.append)

    assert create_cli.create("Demo") == 0
    assert delete_cli.delete("Demo") == 0
    assert calls == [pm_env, pm_env]
