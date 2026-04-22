from pathlib import Path

import pytest

from project_manager import config
from project_manager.paths import Paths


@pytest.fixture
def pm_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Paths:
    repos = tmp_path / "repos"
    worktrees = tmp_path / "worktrees"
    projects = tmp_path / "projects"
    repos.mkdir()
    worktrees.mkdir()
    projects.mkdir()

    cfg = tmp_path / "pm.toml"
    cfg.write_text(
        "[paths]\n"
        f'repos = "{repos}"\n'
        f'worktrees = "{worktrees}"\n'
        f'projects = "{projects}"\n'
    )
    monkeypatch.setenv("PM_CONFIG", str(cfg))
    return config.load()
