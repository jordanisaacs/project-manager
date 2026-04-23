from pathlib import Path

import pytest

from project_manager import config


def test_defaults_when_no_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PM_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    paths = config.load()
    assert paths.repos == tmp_path / ".repos"
    assert paths.worktrees == tmp_path / ".worktrees"
    assert paths.projects == tmp_path / ".projects"
    assert paths.stacker_root == tmp_path / ".stacker"
    assert paths.stacker_db() == tmp_path / ".stacker" / "db.sqlite"
    assert paths.pool_db() == tmp_path / ".worktrees" / "pool.db"


def test_pm_config_overrides(pm_env) -> None:
    assert pm_env.repos.is_dir()
    assert pm_env.worktrees.is_dir()
    assert pm_env.projects.is_dir()
    assert pm_env.stacker_root.is_dir()


def test_xdg_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PM_CONFIG", raising=False)
    xdg = tmp_path / "xdg"
    (xdg / "pm").mkdir(parents=True)
    (xdg / "pm" / "config.toml").write_text(
        f'[paths]\nrepos = "{tmp_path}/r"\nworktrees = "{tmp_path}/w"\nprojects = "{tmp_path}/p"\n'
        f'stacker_root = "{tmp_path}/s"\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    paths = config.load()
    assert paths.repos == tmp_path / "r"
    assert paths.stacker_root == tmp_path / "s"
