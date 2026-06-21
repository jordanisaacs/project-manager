"""`--json` envelope shared by every mutating stacker command (`emit_result`)."""

import json

import pytest

from project_manager import config as pm_config
from project_manager.cli._shared import RepoFlag
from project_manager.paths import Paths
from project_manager.stacker.commands import _common
from project_manager.stacker.commands.pr.unlink import unlink
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import OperationState


@pytest.fixture(autouse=True)
def _wire_config_load(pm_env: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
    """`config.load()` inside the CLI returns this test's Paths."""
    monkeypatch.setattr(pm_config, "load", lambda: pm_env)


def test_command_json_envelope_shape(capsys: pytest.CaptureFixture[str]) -> None:
    """A command's `--json` output is the uniform envelope, operation None."""
    capsys.readouterr()
    rc = unlink(branch="feat", flag=RepoFlag(repo="demo"), json=True)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["command"] == "pr-unlink"
    assert payload["repo"] == "demo"
    assert payload["branch"] == "feat"
    assert "no PR link" in payload["message"]
    assert payload["operation"] is None


def test_envelope_surfaces_paused_operation(
    pm_env: Paths, capsys: pytest.CaptureFixture[str]
) -> None:
    """`emit_result` attaches the repo's paused-op state to the envelope.

    This is what lets a client (the Emacs buffer) surface a conflict and
    offer continue/abort after a sync/absorb/reparent pauses.
    """
    StackerDB(pm_env.stacker_db()).put_operation(
        OperationState(
            repo_name="demo",
            op_type="sync",
            status="paused",
            branch="feat",
            error_message="cherry-pick conflict",
        )
    )
    capsys.readouterr()
    rc = _common.emit_result(
        "paused on feat",
        json=True,
        command="sync",
        repo="demo",
        branch="feat",
        paths=pm_env,
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == {
        "op_type": "sync",
        "status": "paused",
        "branch": "feat",
        "error_message": "cherry-pick conflict",
    }


def test_emit_result_text_mode_unchanged(pm_env: Paths, capsys: pytest.CaptureFixture[str]) -> None:
    """Without `--json`, `emit_result` is the plain markup path (no envelope)."""
    capsys.readouterr()
    rc = _common.emit_result(
        "hello", json=False, command="sync", repo="demo", branch="feat", paths=pm_env
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip() == "hello"
