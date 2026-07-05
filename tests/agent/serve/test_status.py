"""Status-derivation tests.

Ported from openui `sessionStatus.test.ts` (activity-gating) and
db-agents `pluginStatus.test.ts` (status→status/activity table).
"""

import pytest

from project_manager.agent.serve import status as st

# -- event→status mapping (db-agents pluginStatus.test.ts) --------------------


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        ("running", st.WORKING),
        ("post_tool", st.WORKING),
        ("pre_tool", st.WORKING),
        ("compacting", st.WORKING),
        ("waiting_input", st.WAITING_RESPONSE),
        ("permission_request", st.WAITING_PERMISSION),
        ("idle", st.IDLE),
        ("disconnected", st.IDLE),
    ],
)
def test_status_for_known_reports(report: str, expected: str) -> None:
    assert st.status_for(report) == expected


def test_status_for_notification_and_unknown_leave_status_unchanged() -> None:
    # `notification` is ambiguous, unknown words shouldn't flip status.
    assert st.status_for("notification") is None
    assert st.status_for("totally-made-up") is None
    assert st.status_for(None) is None


# -- granular activity (db-agents pluginStatus.test.ts) -----------------------


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("Read", "reading"),
        ("Glob", "reading"),
        ("Grep", "reading"),
        ("Edit", "writing"),
        ("Write", "writing"),
        ("Bash", "running_command"),
        ("Agent", "spawning_agent"),
        ("Task", "spawning_agent"),
    ],
)
def test_activity_for_pre_tool_maps_tool(tool: str, expected: str) -> None:
    assert st.activity_for("pre_tool", tool) == expected


def test_activity_for_pre_tool_unknown_or_missing_tool_is_working() -> None:
    assert st.activity_for("pre_tool", "Mcp__weird") == st.WORKING
    assert st.activity_for("pre_tool", None) == st.WORKING


def test_activity_for_non_pre_tool_reports_coarse_status() -> None:
    assert st.activity_for("waiting_input", None) == st.WAITING_RESPONSE
    assert st.activity_for("permission_request", None) == st.WAITING_PERMISSION
    assert st.activity_for("idle", None) == st.IDLE


# -- activity-gating allowlist (openui sessionStatus.test.ts) -----------------


@pytest.mark.parametrize(
    "event",
    [
        "PreToolUse",
        "PostToolUse",
        "UserPromptSubmit",
        "Stop",
        "SubagentStart",
        "SubagentStop",
        "PreCompact",
        "PermissionRequest",
    ],
)
def test_activity_events_bump_timestamp(event: str) -> None:
    assert st.is_activity_event(event)
    assert st.bump_last_activity_if_activity(1000, event, 5000) == 5000


@pytest.mark.parametrize("event", ["SessionStart", "SessionEnd", "Notification", "", None])
def test_boot_resume_noise_preserves_timestamp(event: str | None) -> None:
    # The load-bearing regression: these fire on boot/auto-resume and must
    # not clobber the prior activity time.
    assert not st.is_activity_event(event)
    assert st.bump_last_activity_if_activity(1000, event, 5000) == 1000


def test_session_end_is_terminal() -> None:
    assert st.is_terminal_event("SessionEnd")


@pytest.mark.parametrize("event", ["SessionStart", "Stop", "PreToolUse", "", None])
def test_non_session_end_is_not_terminal(event: str | None) -> None:
    assert not st.is_terminal_event(event)


@pytest.mark.parametrize(
    "event",
    [
        "preToolUse",
        "postToolUse",
        "beforeSubmitPrompt",
        "stop",
        "preCompact",
        "beforeShellExecution",
        "afterShellExecution",
        "beforeMCPExecution",
        "afterMCPExecution",
        "afterAgentResponse",
        "afterAgentThought",
    ],
)
def test_cursor_camelcase_events_count_as_activity(event: str) -> None:
    assert st.is_activity_event(event)


def test_cursor_session_end_is_terminal() -> None:
    assert st.is_terminal_event("sessionEnd")


def test_cursor_session_start_not_activity() -> None:
    # sessionStart is boot noise — must not bump last_activity.
    assert not st.is_activity_event("sessionStart")
