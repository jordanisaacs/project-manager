"""Pure status-derivation functions for `pm agent serve`.

Ported from the three reference trackers studied while planning:
openui's `sessionStatus.ts` (activity-gating allowlist), tributary's
`hooks.rs` (event→status word), and db-agents' `pluginStatus.ts`
(status→activity + granular tool mapping). Kept side-effect-free so the
store and the tests share one surface.

Status vocabulary (what an observer renders):
    working  — generating / running a tool
    blocked  — waiting for the user (input or approval)
    idle     — turn finished, ready for input
    unknown  — no signal yet
"""

from pathlib import Path

from project_manager.paths import Paths
from project_manager.project import current

WORKING = "working"
# Two distinct "waiting on the user" states — the UI colors them differently:
# the agent asked a question (waiting_response) vs. it wants approval to run a
# tool (waiting_permission).
WAITING_RESPONSE = "waiting_response"
WAITING_PERMISSION = "waiting_permission"
IDLE = "idle"
UNKNOWN = "unknown"

# The status word a hook reports (status-reporter's first arg) → our
# vocabulary. `notification` is intentionally absent: it fires for attention
# pings that may or may not mean "waiting", so it leaves the prior status
# untouched (see `status_for`).
_REPORT_STATUS: dict[str, str] = {
    "running": WORKING,
    "pre_tool": WORKING,
    "post_tool": WORKING,
    "compacting": WORKING,
    "waiting_input": WAITING_RESPONSE,
    "permission_request": WAITING_PERMISSION,
    "idle": IDLE,
    "disconnected": IDLE,
}


def status_for(report_status: str | None) -> str | None:
    """Map a hook's reported status word to our vocabulary.

    Returns `None` when the report should not change the stored status
    (unknown words and `notification` — the latter is ambiguous, so we
    keep whatever the session was last known to be).
    """
    if report_status is None:
        return None
    return _REPORT_STATUS.get(report_status)


# Hook events that represent real agent activity. Only these bump
# `last_activity_at`. `SessionStart`/`SessionEnd`/`Notification` are
# excluded: they fire on boot / auto-resume / attention pings and would
# clobber a restored timestamp with "now" (openui's load-bearing
# regression).
_ACTIVITY_EVENTS: frozenset[str] = frozenset(
    {
        # Claude and Codex name these events in PascalCase.
        "PreToolUse",
        "PostToolUse",
        "UserPromptSubmit",
        "Stop",
        "SubagentStart",
        "SubagentStop",
        "PreCompact",
        "PermissionRequest",
        # Cursor names the equivalent events in camelCase, same gating intent.
        "preToolUse",
        "postToolUse",
        "beforeSubmitPrompt",
        "stop",
        "subagentStart",
        "subagentStop",
        "preCompact",
        "beforeShellExecution",
        "afterShellExecution",
        "beforeMCPExecution",
        "afterMCPExecution",
        "afterAgentResponse",
        "afterAgentThought",
    },
)


def is_activity_event(hook_event: str | None) -> bool:
    """True if `hook_event` represents real agent activity (gating set)."""
    return hook_event is not None and hook_event in _ACTIVITY_EVENTS


# Hook events that mean the session has ended — the daemon drops the row
# (rather than leaving a permanent idle/disconnected ghost).
_TERMINAL_EVENTS: frozenset[str] = frozenset({"SessionEnd", "sessionEnd"})


def is_terminal_event(hook_event: str | None) -> bool:
    """True if `hook_event` means the session has ended and should be removed."""
    return hook_event is not None and hook_event in _TERMINAL_EVENTS


def bump_last_activity_if_activity(prev_ms: int, hook_event: str | None, now_ms: int) -> int:
    """Return `now_ms` for an activity event, else the unchanged `prev_ms`.

    This is the single gate that keeps boot/resume noise from promoting a
    session to the top of a "last active" ordering.
    """
    if is_activity_event(hook_event):
        return now_ms
    return prev_ms


# db-agents `pluginStatus.ts`: a `pre_tool` event carries the tool name,
# which we map to a granular activity for richer display. Unknown / absent
# tools fall through to the coarse `working`.
_TOOL_ACTIVITY: dict[str, str] = {
    "Read": "reading",
    "Glob": "reading",
    "Grep": "reading",
    "Edit": "writing",
    "Write": "writing",
    "NotebookEdit": "writing",
    "Bash": "running_command",
    "Agent": "spawning_agent",
    "Task": "spawning_agent",
}


def activity_for(report_status: str | None, tool: str | None) -> str | None:
    """Derive a granular activity label, or None to leave it unchanged.

    Only `pre_tool` reports carry a meaningful tool; for other reports we
    return the coarse status word (working/blocked/idle) as the activity
    so the field always reflects the latest signal.
    """
    if report_status == "pre_tool" and tool:
        return _TOOL_ACTIVITY.get(tool, WORKING)
    return status_for(report_status)


def cwd_to_project(paths: Paths, cwd: str | None) -> str | None:
    """Resolve a session's recorded cwd to a pm project name, or None.

    Reuses the reverse helpers `pm` already uses for `detect_current_project`
    so the mapping matches `pm agent ls`. Tries the logical `~/.projects/<name>`
    layout first, then the resolved `~/.worktrees/<repo>/<uuid>` layout (the
    form an agent usually records after symlink resolution).
    """
    if not cwd:
        return None
    path = Path(cwd)
    return current.project_from_projects_path(path, paths) or current.project_from_worktree_path(
        path,
        paths,
    )
