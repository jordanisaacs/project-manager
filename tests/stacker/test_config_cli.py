from __future__ import annotations

import argparse

import pytest

from project_manager import config as pm_config
from project_manager.paths import Paths
from project_manager.stacker import cli


@pytest.fixture(autouse=True)
def _wire_config_load(pm_env: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure `config.load()` inside CLI returns this test's Paths."""
    monkeypatch.setattr(pm_config, "load", lambda: pm_env)


def _ns(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)


def _run(args: argparse.Namespace) -> int:
    return cli._cmd_config(args)


def test_get_unset_key_exits_1() -> None:
    code = _run(_ns(repo="demo", list=False, unset=False, key="pr.mode", value=None))
    assert code == 1


def test_set_then_get(capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(_ns(repo="demo", list=False, unset=False, key="pr.trunk", value="master")) == 0
    capsys.readouterr()
    assert _run(_ns(repo="demo", list=False, unset=False, key="pr.trunk", value=None)) == 0
    assert capsys.readouterr().out.strip() == "master"


def test_list_prints_sorted_key_equals_value(capsys: pytest.CaptureFixture[str]) -> None:
    _run(_ns(repo="demo", list=False, unset=False, key="pr.trunk", value="master"))
    _run(_ns(repo="demo", list=False, unset=False, key="pr.mode", value="repo-pr"))
    capsys.readouterr()
    assert _run(_ns(repo="demo", list=True, unset=False, key=None, value=None)) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["pr.mode=repo-pr", "pr.trunk=master"]


def test_unset_missing_key_exits_1() -> None:
    assert _run(_ns(repo="demo", list=False, unset=True, key="pr.trunk", value=None)) == 1


def test_unset_existing_key_exits_0_and_clears() -> None:
    _run(_ns(repo="demo", list=False, unset=False, key="pr.trunk", value="master"))
    assert _run(_ns(repo="demo", list=False, unset=True, key="pr.trunk", value=None)) == 0
    assert _run(_ns(repo="demo", list=False, unset=False, key="pr.trunk", value=None)) == 1


def test_unknown_key_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    code = _run(_ns(repo="demo", list=False, unset=False, key="nope", value="x"))
    assert code == 2
    assert "Unknown config key" in capsys.readouterr().err
