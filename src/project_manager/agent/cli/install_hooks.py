"""`pm agent install-hooks` — install best-effort status hooks for agents.

Writes a single shared reporter script and registers it with Claude
(`~/.claude/settings.json`) and Codex (`~/.codex/hooks.json`) at the user
level so it loads for every session, no per-project trust prompt. The
reporter is fire-and-forget: a short-timeout POST to the daemon, falling
back to a debug logfile, always exiting 0 so it never blocks the agent.

Idempotent: re-running replaces only our own entries (matched by the
script path) and leaves any other user hooks untouched.
"""

import json
import stat
from pathlib import Path
from typing import Annotated, Any

from cyclopts import Parameter

from project_manager import config

from . import agent_app

_SCRIPT_REL = Path(".pm/hooks/status-reporter.py")
_DEBUG_LOG = "~/.pm/hook-debug.log"
# Codex profile that carries our hooks, layered via `codex --profile pm`.
# A profile config lives in its own file that `isaac codex` never rewrites,
# unlike the managed ~/.codex/hooks.json.
_CODEX_PROFILE_NAME = "pm"
_CODEX_PROFILE_REL = Path(".codex") / f"{_CODEX_PROFILE_NAME}.config.toml"
# Recognize our own hook entries by this path stem regardless of extension,
# so re-running after the legacy .sh → .py switch replaces rather than
# duplicates them.
_HOOK_STEM = str(Path.home() / ".pm" / "hooks" / "status-reporter")

# (claude_event, matcher, status-word). Codex supports only a subset of
# Claude's lifecycle events (see `_CODEX_SKIP`); both share the rest.
_EVENT_STATUS: tuple[tuple[str, str, str], ...] = (
    ("SessionStart", "*", "idle"),
    ("UserPromptSubmit", "*", "running"),
    ("PreToolUse", "AskUserQuestion", "waiting_input"),
    ("PreToolUse", "*", "pre_tool"),
    ("PostToolUse", "*", "post_tool"),
    ("Stop", "*", "idle"),
    ("SubagentStart", "*", "running"),
    ("SubagentStop", "*", "running"),
    ("PreCompact", "*", "compacting"),
    ("PermissionRequest", "*", "permission_request"),
    ("Notification", "*", "notification"),
    ("SessionEnd", "*", "disconnected"),
)
# Codex's hook framework supports only PreToolUse / PostToolUse /
# PermissionRequest / UserPromptSubmit / Stop / SessionStart, so skip the
# events it has no equivalent for. (SessionEnd has no Codex analog either —
# Codex sessions are cleaned up by the daemon's pid-liveness backstop.)
_CODEX_SKIP = frozenset(
    {"Notification", "SessionEnd", "SubagentStart", "SubagentStop", "PreCompact"},
)

# Cursor uses its own camelCase lifecycle events and a flatter hooks.json shape
# (see https://cursor.com/docs/hooks). (cursor_event, matcher-or-None, status).
# Cursor's hook payload carries `session_id` (== conversation_id) and
# `transcript_path`, so the shared reporter reads it unchanged; only `cwd` needs
# a `workspace_roots` fallback (Cursor omits cwd at sessionStart).
_CURSOR_EVENT_STATUS: tuple[tuple[str, str | None, str], ...] = (
    ("sessionStart", None, "idle"),
    ("beforeSubmitPrompt", None, "running"),
    ("preToolUse", ".*", "pre_tool"),
    # Shell/MCP execution gates fire after preToolUse and *before* the agent
    # pauses for the user to approve — so they're our "waiting for approval"
    # signal. The matching after* events transition back to working once the
    # user approves (or in auto mode, immediately).
    ("beforeShellExecution", None, "permission_request"),
    ("beforeMCPExecution", None, "permission_request"),
    ("afterShellExecution", None, "post_tool"),
    ("afterMCPExecution", None, "post_tool"),
    ("postToolUse", ".*", "post_tool"),
    ("subagentStart", None, "running"),
    ("subagentStop", None, "running"),
    ("preCompact", None, "compacting"),
    ("stop", None, "idle"),
    ("sessionEnd", None, "disconnected"),
    # Cursor exposes no clean "waiting for the user" hook, but it does signal
    # when the agent *pauses*: an interactive option picker keeps the turn
    # active and its last event is `afterAgentThought` (no `stop`), which would
    # otherwise leave the stale "working" from `beforeSubmitPrompt`. Map both
    # pause events to idle so a parked agent never reads as working. (They race
    # `stop` on the wire, but both resolve to idle, so order doesn't matter; a
    # following tool/prompt event flips back to working.)
    ("afterAgentResponse", None, "idle"),
    ("afterAgentThought", None, "idle"),
)

# Shared reporter — stdlib Python (no jq/curl/bash-quoting traps, and a
# non-blocking stdin read so a hook handed a tty stdin can't hang the agent).
# `__PM_SERVE_PORT__` is substituted at install; the live env var still wins
# so an operator can repoint it without reinstalling.
_SCRIPT_TEMPLATE = '''#!/usr/bin/env python3
"""pm agent serve status reporter. Args: <status-word> <agent>.

Best-effort: never blocks, short timeout, debug-log on failure, exit 0.
"""
import json
import os
import select
import sys
import urllib.request
from datetime import datetime, timezone

PORT = os.environ.get("PM_SERVE_PORT", "__PM_SERVE_PORT__")
LOG = os.path.expanduser(os.environ.get("PM_HOOK_DEBUG_LOG", "__PM_DEBUG_LOG__"))


def read_stdin(timeout=2.0):
    """Read the hook JSON from stdin without ever blocking the agent.

    Hooks get their payload on a pipe that is closed after the write. If a
    wrapper instead hands the agent's tty to the hook, reading would block
    forever — so skip a tty, and otherwise only read once data is ready.
    """
    try:
        if sys.stdin.isatty():
            return ""
        if select.select([sys.stdin], [], [], timeout)[0]:
            return sys.stdin.read()
    except Exception:
        pass
    return ""


def _ppid_of(pid):
    # Field 4 of /proc/<pid>/stat is the parent pid. comm (field 2) is
    # parenthesized and may contain spaces, so split on the last ')'.
    with open("/proc/%d/stat" % pid) as fh:
        return int(fh.read().rpartition(")")[2].split()[1])


def _comm_of(pid):
    with open("/proc/%d/comm" % pid) as fh:
        return fh.read().strip()


def agent_pid():
    """PID of the agent process running this hook, for a liveness backstop.

    The hook is a child of the agent, but the agent may invoke it through a
    transient `sh -c` wrapper. Walk past such a shell so the reported PID is
    the long-lived agent (e.g. claude/node), which exists for exactly the
    session's lifetime — unlike the terminal buffer, which outlives it.
    """
    try:
        pid = os.getppid()
        if _comm_of(pid) in ("sh", "bash", "zsh", "dash", "fish"):
            parent = _ppid_of(pid)
            if parent > 1:
                pid = parent
        return pid
    except Exception:
        return os.getppid()


def main():
    status = sys.argv[1] if len(sys.argv) > 1 else ""
    agent = sys.argv[2] if len(sys.argv) > 2 else ""
    try:
        data = json.loads(read_stdin() or "{}")
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    # Cursor runs Claude Code-style hooks in addition to its own, so our
    # agent=claude hook also fires inside a Cursor session. Cursor's payload
    # always carries `cursor_version` (Claude's never does); when we see it on a
    # non-cursor invocation, skip — Cursor already self-reports as agent=cursor,
    # and reporting again would create a duplicate (claude, <cursor-id>) row.
    if agent != "cursor" and data.get("cursor_version"):
        return
    meta = {k[len("PM_META_"):]: v for k, v in os.environ.items()
            if k.startswith("PM_META_")}
    payload = {
        "status": status,
        "agent": agent,
        "meta": meta,
        "session_id": data.get("session_id", ""),
        # Cursor omits `cwd` on some events but always sends `workspace_roots`.
        "cwd": data.get("cwd") or (data.get("workspace_roots") or [""])[0],
        "hook_event_name": data.get("hook_event_name", ""),
        # Codex SessionStart includes `source` ("startup", "resume", "clear",
        # "compact"). `source=clear` is the privacy-preserving signal that a
        # `/clear` replaced the old thread; do not forward the raw prompt.
        "source": data.get("source", ""),
        "tool_name": data.get("tool_name", ""),
        "model": data.get("model", ""),
        "transcript_path": data.get("transcript_path", ""),
        "pid": agent_pid(),
    }
    req = urllib.request.Request(
        "http://127.0.0.1:%s/api/status" % PORT,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=2).read()
    except Exception as exc:
        try:
            with open(LOG, "a") as fh:
                fh.write("%s %s %s %s %s\\n" % (
                    datetime.now(timezone.utc).isoformat(),
                    agent, status, exc, json.dumps(payload)))
        except Exception:
            pass


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
    claude: Annotated[bool, Parameter(help="install Claude hooks")] = True,
    codex: Annotated[bool, Parameter(help="install Codex hooks")] = True,
    cursor: Annotated[bool, Parameter(help="install Cursor hooks")] = True,
    port: Annotated[
        int | None, Parameter(help="override [serve].port baked into the script")
    ] = None,
) -> int:
    """Install the best-effort status-reporter hooks for Claude, Codex, and Cursor."""
    cfg = config.serve()
    bind_port = port if port is not None else cfg.port

    script = _write_script(bind_port)
    print(f"pm agent install-hooks: wrote reporter → {script}")

    if claude:
        target = Path.home() / ".claude" / "settings.json"
        _merge_hooks(target, _hooks_mapping(script, "claude", skip=frozenset()))
        print(f"pm agent install-hooks: registered Claude hooks → {target}")
    if cursor:
        # Cursor reads ~/.cursor/hooks.json directly (not via isaac), so a
        # straight merge sticks — no profile/clobber dance like Codex.
        target = Path.home() / ".cursor" / "hooks.json"
        _merge_cursor_hooks(target, _cursor_hooks_mapping(script))
        print(f"pm agent install-hooks: registered Cursor hooks → {target}")
    if codex:
        # Codex's user-level ~/.codex/hooks.json is owned and regenerated by
        # `isaac codex` on every launch (it strips any hook it didn't write).
        # So install into a *profile* config instead — `codex --profile pm`
        # layers ~/.codex/pm.config.toml on top of the managed config, and
        # isaac never touches profile files. Codex loads all hook layers, so
        # our reporter runs alongside isaac's managed hooks.
        target = _write_codex_profile(script)
        print(f"pm agent install-hooks: registered Codex hooks → {target}")
        print(
            "  ↳ launch Codex with `--profile "
            f"{_CODEX_PROFILE_NAME}` (e.g. `isaac codex -- --profile {_CODEX_PROFILE_NAME}`),\n"
            "    then run `/hooks` in Codex once to trust the pm reporter.",
        )
    return 0


def _write_script(port: int) -> Path:
    path = Path.home() / _SCRIPT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _SCRIPT_TEMPLATE.replace("__PM_SERVE_PORT__", str(port)).replace(
        "__PM_DEBUG_LOG__",
        str(Path(_DEBUG_LOG).expanduser()),
    )
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    # Remove the legacy bash reporter so it can't linger alongside the new one.
    (path.parent / "status-reporter.sh").unlink(missing_ok=True)
    return path


def _codex_profile_toml(script: Path) -> str:
    """Render the Codex profile TOML carrying the status-reporter hooks.

    Mirrors the `{event: [{matcher?, hooks: [...]}]}` shape Codex reads from
    an inline `[hooks]` table, built from the same `_EVENT_STATUS` table as the
    Claude install (minus the events Codex has no equivalent for).
    """
    lines = [
        "# Managed by `pm agent install-hooks` — do not edit by hand.",
        f"# Layered via `codex --profile {_CODEX_PROFILE_NAME}`; isaac never",
        "# rewrites profile files, so the pm status reporter survives its",
        "# regeneration of ~/.codex/hooks.json.",
        "",
    ]
    for event, matcher, status in _EVENT_STATUS:
        if event in _CODEX_SKIP:
            continue
        lines.append(f"[[hooks.{event}]]")
        if matcher != "*":
            lines.append(f'matcher = "{matcher}"')
        lines.append(f"[[hooks.{event}.hooks]]")
        lines.append('type = "command"')
        lines.append(f'command = "{script} {status} codex"')
        lines.append("timeout = 5")
        lines.append("")
    return "\n".join(lines) + "\n"


def _write_codex_profile(script: Path) -> Path:
    path = Path.home() / _CODEX_PROFILE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_codex_profile_toml(script))
    return path


def _hooks_mapping(
    script: Path, agent: str, *, skip: frozenset[str]
) -> dict[str, list[dict[str, Any]]]:
    """Build the `{event: [{matcher, hooks:[...]}]}` mapping for one agent."""
    out: dict[str, list[dict[str, Any]]] = {}
    for event, matcher, status in _EVENT_STATUS:
        if event in skip:
            continue
        out.setdefault(event, []).append(
            {
                "matcher": matcher,
                "hooks": [
                    {"type": "command", "command": f"{script} {status} {agent}", "timeout": 5},
                ],
            },
        )
    return out


def _cursor_hooks_mapping(script: Path) -> dict[str, list[dict[str, Any]]]:
    """Build Cursor's `{event: [{command, matcher?, timeout}]}` mapping.

    Cursor's entries are flat (a bare `command` string, no nested `hooks`),
    so this is a separate shape from `_hooks_mapping`.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for event, matcher, status in _CURSOR_EVENT_STATUS:
        entry: dict[str, Any] = {"command": f"{script} {status} cursor", "timeout": 5}
        if matcher is not None:
            entry["matcher"] = matcher
        out.setdefault(event, []).append(entry)
    return out


def _merge_cursor_hooks(target: Path, mapping: dict[str, list[dict[str, Any]]]) -> None:
    """Merge Cursor hooks into `target`, replacing our entries, keeping others.

    Same back-up-once + replace-ours strategy as `_merge_hooks`, but for
    Cursor's flat entry shape and its top-level `version` key.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if target.exists():
        backup = target.with_suffix(target.suffix + ".pm-bak")
        if not backup.exists():
            backup.write_text(target.read_text())
        try:
            data = json.loads(target.read_text() or "{}")
        except json.JSONDecodeError:
            data = {}
    data.setdefault("version", 1)
    hooks: dict[str, list[dict[str, Any]]] = data.get("hooks", {})
    for event, entries in mapping.items():
        kept = [e for e in hooks.get(event, []) if _HOOK_STEM not in str(e.get("command", ""))]
        hooks[event] = kept + entries
    data["hooks"] = hooks
    target.write_text(json.dumps(data, indent=2) + "\n")


def _is_ours(entry: dict[str, Any]) -> bool:
    return any(_HOOK_STEM in str(h.get("command", "")) for h in entry.get("hooks", []))


def _merge_hooks(
    target: Path,
    mapping: dict[str, list[dict[str, Any]]],
) -> None:
    """Merge `mapping` under `target`'s top-level `hooks`, replacing our entries.

    Backs up the file once (`.pm-bak`) the first time we touch it, then for
    each event drops any prior entries that point at our script and appends
    the fresh ones — leaving unrelated user hooks intact.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if target.exists():
        backup = target.with_suffix(target.suffix + ".pm-bak")
        if not backup.exists():
            backup.write_text(target.read_text())
        try:
            data = json.loads(target.read_text() or "{}")
        except json.JSONDecodeError:
            data = {}
    hooks: dict[str, list[dict[str, Any]]] = data.get("hooks", {})
    for event, entries in mapping.items():
        kept = [e for e in hooks.get(event, []) if not _is_ours(e)]
        hooks[event] = kept + entries
    data["hooks"] = hooks
    target.write_text(json.dumps(data, indent=2) + "\n")
