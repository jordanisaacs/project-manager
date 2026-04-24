import pytest

from project_manager import config as pm_config
from project_manager.cli._shared import RepoFlag
from project_manager.paths import Paths
from project_manager.stacker.commands.config import config as config_cmd


@pytest.fixture(autouse=True)
def _wire_config_load(pm_env: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure `config.load()` inside CLI returns this test's Paths."""
    monkeypatch.setattr(pm_config, "load", lambda: pm_env)


def _run(**kwargs) -> int:
    flag = RepoFlag(repo=kwargs.pop("repo", "demo"))
    return config_cmd(flag=flag, **kwargs)


def test_get_unset_key_exits_1() -> None:
    assert _run(key="pr.mode") == 1


def test_set_then_get(capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(key="pr.trunk", value="master") == 0
    capsys.readouterr()
    assert _run(key="pr.trunk") == 0
    assert capsys.readouterr().out.strip() == "master"


def test_list_prints_sorted_keys_and_values(capsys: pytest.CaptureFixture[str]) -> None:
    _run(key="pr.trunk", value="master")
    _run(key="pr.mode", value="repo-pr")
    capsys.readouterr()
    assert _run(list_=True) == 0
    out = capsys.readouterr().out
    # pr.mode comes before pr.trunk alphabetically; both keys and their
    # values appear in the rendered table.
    mode_idx = out.find("pr.mode")
    trunk_idx = out.find("pr.trunk")
    assert 0 <= mode_idx < trunk_idx
    assert "repo-pr" in out
    assert "master" in out


def test_list_json(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    _run(key="pr.trunk", value="master")
    _run(key="pr.mode", value="repo-pr")
    capsys.readouterr()
    assert _run(list_=True, json=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == [
        {"key": "pr.mode",  "value": "repo-pr"},
        {"key": "pr.trunk", "value": "master"},
    ]


def test_unset_missing_key_exits_1() -> None:
    assert _run(unset=True, key="pr.trunk") == 1


def test_unset_existing_key_exits_0_and_clears() -> None:
    _run(key="pr.trunk", value="master")
    assert _run(unset=True, key="pr.trunk") == 0
    assert _run(key="pr.trunk") == 1


def test_unknown_key_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    # Routed through `main()` to exercise the global CommandError handler.
    from project_manager.cli import main
    code = main(["stacker", "config", "--repo", "demo", "nope", "x"])
    assert code == 2
    assert "Unknown config key" in capsys.readouterr().err
