"""Install identity-only hooks for the Emacs Ghostel agent tracker.

The generated reporter sends a small base64 JSON identity payload directly to
the Emacs daemon named by ``PM_META_SERVER``.  Hooks never report status: the
Ghostel active screen and foreground process group remain authoritative.
"""

import json
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import agent_app

_SCRIPT_REL = Path(".pm/hooks/identity-reporter.py")
_CODEX_PROFILE_NAME = "pm"
_CODEX_PROFILE_REL = Path(".codex") / f"{_CODEX_PROFILE_NAME}.config.toml"

# Recognize every pm reporter generation so a reinstall removes stale status
# hooks even from lifecycle events that identity reporting no longer uses.
_HOOK_STEMS = (
    str(Path.home() / ".pm" / "hooks" / "identity-reporter"),
    str(Path.home() / ".pm" / "hooks" / "status-reporter"),
)

_EVENTS = ("SessionStart", "UserPromptSubmit")
_CURSOR_EVENTS = ("sessionStart", "beforeSubmitPrompt")

_SCRIPT_TEMPLATE = '''#!/usr/bin/env python3
"""Deliver coding-agent identity to an Emacs-local Ghostel tracker."""
import base64
import json
import os
import select
import subprocess
import sys


def read_stdin(timeout=2.0):
    """Read hook JSON without hanging when a wrapper supplies a tty."""
    try:
        if sys.stdin.isatty():
            return ""
        if select.select([sys.stdin], [], [], timeout)[0]:
            return sys.stdin.read()
    except Exception:
        pass
    return ""


def main():
    agent = sys.argv[1] if len(sys.argv) > 1 else ""
    event = sys.argv[2] if len(sys.argv) > 2 else ""
    server = os.environ.get("PM_META_SERVER", "")
    buffer_id = os.environ.get("PM_META_BUF", "")
    if not server or not buffer_id or agent not in ("claude", "codex", "cursor"):
        return
    try:
        data = json.loads(read_stdin() or "{}")
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    # Cursor also runs Claude-compatible hooks.  Its native payload includes
    # cursor_version, so suppress the duplicate Claude identity delivery.
    if agent != "cursor" and data.get("cursor_version"):
        return
    payload = {
        "agent": agent,
        "buffer_id": buffer_id,
        "session_id": data.get("session_id", ""),
        "cwd": data.get("cwd") or (data.get("workspace_roots") or [""])[0],
        "hook_event_name": data.get("hook_event_name") or event,
        "source": data.get("source", ""),
        "transcript_path": data.get("transcript_path", ""),
    }
    encoded = base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode("ascii")
    expression = '(pm-agent-track-identity "%s")' % encoded
    subprocess.run(
        ["emacsclient", "--socket-name", server, "--eval", expression],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=2,
        check=False,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
'''


@agent_app.command(name="install-hooks")
def install_hooks(
    *,
    claude: bool = True,
    codex: bool = True,
    cursor: bool = True,
) -> int:
    """Install identity hooks for Claude, Codex, and Cursor."""
    script = _write_script()
    print(f"pm agent install-hooks: wrote identity reporter → {script}")

    if claude:
        target = Path.home() / ".claude" / "settings.json"
        _merge_hooks(target, _hooks_mapping(script, "claude"))
        print(f"pm agent install-hooks: registered Claude identity hooks → {target}")
    if cursor:
        target = Path.home() / ".cursor" / "hooks.json"
        _merge_cursor_hooks(target, _cursor_hooks_mapping(script))
        print(f"pm agent install-hooks: registered Cursor identity hooks → {target}")
    if codex:
        target = _write_codex_profile(script)
        print(f"pm agent install-hooks: registered Codex identity hooks → {target}")
        print(
            "  ↳ launch Codex with `--profile "
            f"{_CODEX_PROFILE_NAME}` (e.g. `isaac codex -- --profile {_CODEX_PROFILE_NAME}`),\n"
            "    then run `/hooks` in Codex once to trust the pm reporter.",
        )
    return 0


def _write_script() -> Path:
    path = Path.home() / _SCRIPT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_SCRIPT_TEMPLATE)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    # Old reporters are no longer referenced after the global hook cleanup.
    (path.parent / "status-reporter.py").unlink(missing_ok=True)
    (path.parent / "status-reporter.sh").unlink(missing_ok=True)
    return path


def _command(script: Path, agent: str, event: str) -> str:
    return f"{script} {agent} {event}"


def _hooks_mapping(script: Path, agent: str) -> dict[str, list[dict[str, Any]]]:
    """Build Claude/Codex's nested hook mapping."""
    return {
        event: [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": _command(script, agent, event),
                        "timeout": 5,
                    },
                ],
            },
        ]
        for event in _EVENTS
    }


def _cursor_hooks_mapping(script: Path) -> dict[str, list[dict[str, Any]]]:
    """Build Cursor's flat hook mapping."""
    return {
        event: [
            {
                "command": _command(script, "cursor", event),
                "timeout": 5,
            },
        ]
        for event in _CURSOR_EVENTS
    }


def _codex_profile_toml(script: Path) -> str:
    """Render the Codex profile containing only identity events."""
    lines = [
        "# Managed by `pm agent install-hooks` — do not edit by hand.",
        f"# Layered via `codex --profile {_CODEX_PROFILE_NAME}`.",
        "",
    ]
    for event in _EVENTS:
        lines.extend(
            (
                f"[[hooks.{event}]]",
                f"[[hooks.{event}.hooks]]",
                'type = "command"',
                f'command = "{_command(script, "codex", event)}"',
                "timeout = 5",
                "",
            ),
        )
    return "\n".join(lines) + "\n"


def _write_codex_profile(script: Path) -> Path:
    path = Path.home() / _CODEX_PROFILE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_codex_profile_toml(script))
    return path


def _command_is_ours(command: object) -> bool:
    text = str(command)
    return any(stem in text for stem in _HOOK_STEMS)


def _nested_entry_is_ours(entry: dict[str, Any]) -> bool:
    return any(_command_is_ours(hook.get("command", "")) for hook in entry.get("hooks", []))


def _read_json_with_backup(target: Path) -> dict[str, Any]:
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        return {}
    backup = target.with_suffix(target.suffix + ".pm-bak")
    if not backup.exists():
        backup.write_text(target.read_text())
    try:
        data = json.loads(target.read_text() or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _clean_all_events(
    hooks: dict[str, list[dict[str, Any]]],
    predicate: Callable[[dict[str, Any]], bool],
) -> None:
    """Remove reporter entries from every event, including obsolete events."""
    for event in list(hooks):
        kept = [entry for entry in hooks[event] if not predicate(entry)]
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event)


def _merge_hooks(target: Path, mapping: dict[str, list[dict[str, Any]]]) -> None:
    """Merge nested hooks after removing all old pm reporter entries."""
    data = _read_json_with_backup(target)
    hooks: dict[str, list[dict[str, Any]]] = data.get("hooks", {})
    _clean_all_events(hooks, _nested_entry_is_ours)
    for event, entries in mapping.items():
        hooks[event] = hooks.get(event, []) + entries
    data["hooks"] = hooks
    target.write_text(json.dumps(data, indent=2) + "\n")


def _merge_cursor_hooks(target: Path, mapping: dict[str, list[dict[str, Any]]]) -> None:
    """Merge flat Cursor hooks after removing all old reporter entries."""
    data = _read_json_with_backup(target)
    data.setdefault("version", 1)
    hooks: dict[str, list[dict[str, Any]]] = data.get("hooks", {})
    _clean_all_events(hooks, lambda entry: _command_is_ours(entry.get("command", "")))
    for event, entries in mapping.items():
        hooks[event] = hooks.get(event, []) + entries
    data["hooks"] = hooks
    target.write_text(json.dumps(data, indent=2) + "\n")
