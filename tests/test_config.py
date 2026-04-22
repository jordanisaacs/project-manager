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


def test_pm_config_overrides(pm_env) -> None:
    assert pm_env.repos.is_dir()
    assert pm_env.worktrees.is_dir()
    assert pm_env.projects.is_dir()


def test_xdg_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("PM_CONFIG", raising=False)
    xdg = tmp_path / "xdg"
    (xdg / "pm").mkdir(parents=True)
    (xdg / "pm" / "config.toml").write_text(
        f'[paths]\nrepos = "{tmp_path}/r"\nworktrees = "{tmp_path}/w"\nprojects = "{tmp_path}/p"\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    paths = config.load()
    assert paths.repos == tmp_path / "r"
