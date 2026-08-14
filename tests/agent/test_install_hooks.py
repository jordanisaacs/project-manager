import json
from pathlib import Path

import project_manager.cli  # noqa: F401  # initialize the shared root app first
from project_manager.agent.cli import install_hooks as hooks


def test_identity_reporter_targets_emacs_not_serve() -> None:
    assert "emacsclient" in hooks._SCRIPT_TEMPLATE
    assert "pm-agent-track-identity" in hooks._SCRIPT_TEMPLATE
    assert "/api/status" not in hooks._SCRIPT_TEMPLATE
    assert "status" not in hooks._EVENTS


def test_hook_mappings_install_identity_events_only(tmp_path: Path) -> None:
    script = tmp_path / "identity-reporter.py"
    claude = hooks._hooks_mapping(script, "claude")
    cursor = hooks._cursor_hooks_mapping(script)
    assert set(claude) == {"SessionStart", "UserPromptSubmit"}
    assert set(cursor) == {"sessionStart", "beforeSubmitPrompt"}
    assert "claude SessionStart" in claude["SessionStart"][0]["hooks"][0]["command"]
    assert "cursor beforeSubmitPrompt" in cursor["beforeSubmitPrompt"][0]["command"]


def test_nested_merge_removes_old_reporters_from_obsolete_events(
    tmp_path: Path,
    monkeypatch,
) -> None:
    old_stem = str(tmp_path / ".pm/hooks/status-reporter")
    new_stem = str(tmp_path / ".pm/hooks/identity-reporter")
    monkeypatch.setattr(hooks, "_HOOK_STEMS", (old_stem, new_stem))
    target = tmp_path / "settings.json"
    target.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "matcher": "*",
                            "hooks": [{"command": f"{old_stem}.py idle claude"}],
                        },
                    ],
                    "SessionStart": [
                        {
                            "matcher": "*",
                            "hooks": [{"command": "/usr/local/bin/custom-hook"}],
                        },
                    ],
                },
            },
        ),
    )
    script = Path(f"{new_stem}.py")
    hooks._merge_hooks(target, hooks._hooks_mapping(script, "claude"))
    result = json.loads(target.read_text())["hooks"]
    assert "Stop" not in result
    assert len(result["SessionStart"]) == 2
    assert result["SessionStart"][0]["hooks"][0]["command"] == "/usr/local/bin/custom-hook"


def test_cursor_merge_removes_old_reporters_from_all_events(
    tmp_path: Path,
    monkeypatch,
) -> None:
    old_stem = str(tmp_path / ".pm/hooks/status-reporter")
    new_stem = str(tmp_path / ".pm/hooks/identity-reporter")
    monkeypatch.setattr(hooks, "_HOOK_STEMS", (old_stem, new_stem))
    target = tmp_path / "hooks.json"
    target.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "stop": [{"command": f"{old_stem}.py idle cursor"}],
                    "sessionStart": [{"command": "/usr/local/bin/custom-hook"}],
                },
            },
        ),
    )
    script = Path(f"{new_stem}.py")
    hooks._merge_cursor_hooks(target, hooks._cursor_hooks_mapping(script))
    result = json.loads(target.read_text())["hooks"]
    assert "stop" not in result
    assert len(result["sessionStart"]) == 2
    assert result["sessionStart"][0]["command"] == "/usr/local/bin/custom-hook"


def test_codex_profile_has_no_status_lifecycle_hooks(tmp_path: Path) -> None:
    profile = hooks._codex_profile_toml(tmp_path / "identity-reporter.py")
    assert "hooks.SessionStart" in profile
    assert "hooks.UserPromptSubmit" in profile
    assert "PreToolUse" not in profile
    assert "Stop" not in profile
