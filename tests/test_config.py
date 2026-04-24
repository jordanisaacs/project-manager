import zoneinfo
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


def test_display_defaults_to_none_timezone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("PM_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # No config file at all → system-local behavior.
    assert config.display().timezone is None


def test_display_reads_configured_timezone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    cfg = tmp_path / "pm.toml"
    cfg.write_text(
        '[paths]\nrepos = "/r"\nworktrees = "/w"\nprojects = "/p"\n'
        'stacker_root = "/s"\n'
        '[display]\ntimezone = "America/Los_Angeles"\n',
    )
    monkeypatch.setenv("PM_CONFIG", str(cfg))
    display = config.display()
    assert display.timezone == zoneinfo.ZoneInfo("America/Los_Angeles")


def test_display_rejects_unknown_timezone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    cfg = tmp_path / "pm.toml"
    cfg.write_text(
        '[paths]\nrepos = "/r"\nworktrees = "/w"\nprojects = "/p"\n'
        'stacker_root = "/s"\n'
        '[display]\ntimezone = "Mars/Olympus"\n',
    )
    monkeypatch.setenv("PM_CONFIG", str(cfg))
    with pytest.raises(ValueError, match="unknown timezone"):
        config.display()


def test_agents_defaults_to_empty_commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("PM_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config.agents().commands == {}


def test_agents_reads_commands_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    cfg = tmp_path / "pm.toml"
    cfg.write_text(
        '[paths]\nrepos = "/r"\nworktrees = "/w"\nprojects = "/p"\n'
        'stacker_root = "/s"\n'
        '[agents.commands]\n'
        'claude = "isaac"\n'
        'codex = "isaac codex --"\n'
        'cursor = "agent"\n',
    )
    monkeypatch.setenv("PM_CONFIG", str(cfg))
    cmds = config.agents().commands
    assert cmds == {
        "claude": "isaac",
        "codex": "isaac codex --",
        "cursor": "agent",
    }


def test_agents_rejects_non_string_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    cfg = tmp_path / "pm.toml"
    cfg.write_text(
        '[paths]\nrepos = "/r"\nworktrees = "/w"\nprojects = "/p"\n'
        'stacker_root = "/s"\n'
        '[agents.commands]\nclaude = 42\n',
    )
    monkeypatch.setenv("PM_CONFIG", str(cfg))
    with pytest.raises(ValueError, match=r"\[agents\.commands\]\.claude"):
        config.agents()


def test_concurrency_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.delenv("PM_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert config.concurrency().limit == 10


def test_concurrency_reads_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    cfg = tmp_path / "pm.toml"
    cfg.write_text(
        '[paths]\nrepos = "/r"\nworktrees = "/w"\nprojects = "/p"\n'
        'stacker_root = "/s"\n'
        '[concurrency]\nlimit = 4\n',
    )
    monkeypatch.setenv("PM_CONFIG", str(cfg))
    assert config.concurrency().limit == 4


def test_concurrency_rejects_non_positive_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    cfg = tmp_path / "pm.toml"
    cfg.write_text(
        '[paths]\nrepos = "/r"\nworktrees = "/w"\nprojects = "/p"\n'
        'stacker_root = "/s"\n'
        '[concurrency]\nlimit = 0\n',
    )
    monkeypatch.setenv("PM_CONFIG", str(cfg))
    with pytest.raises(ValueError, match="positive integer"):
        config.concurrency()


def test_concurrency_rejects_non_integer_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    cfg = tmp_path / "pm.toml"
    cfg.write_text(
        '[paths]\nrepos = "/r"\nworktrees = "/w"\nprojects = "/p"\n'
        'stacker_root = "/s"\n'
        '[concurrency]\nlimit = "lots"\n',
    )
    monkeypatch.setenv("PM_CONFIG", str(cfg))
    with pytest.raises(ValueError, match="positive integer"):
        config.concurrency()


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
