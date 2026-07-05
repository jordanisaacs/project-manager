"""Tests for the shared `agent.transcript` title library."""

import json
from pathlib import Path

from project_manager.agent import transcript


def _lines(*objs: dict) -> list[str]:
    return [json.dumps(o) for o in objs]


def test_first_typed_prompt_when_no_titles() -> None:
    lines = _lines(
        {"type": "user", "message": {"role": "user", "content": "build me a thing"}},
        {
            "type": "assistant",
            "message": {"model": "claude-4", "content": [{"type": "text", "text": "ok"}]},
        },
    )
    assert transcript.title_from_lines(lines) == "build me a thing"


def test_synthetic_summary_beats_first_user() -> None:
    lines = _lines(
        {"type": "user", "message": {"content": "original prompt"}},
        {
            "type": "assistant",
            "message": {
                "model": "<synthetic>",
                "content": [{"type": "text", "text": "Summary:\n\nFinal: did a thing."}],
            },
        },
    )
    assert transcript.title_from_lines(lines) == "Final: did a thing."


def test_ai_title_beats_summary_and_first_user() -> None:
    lines = _lines(
        {"type": "user", "message": {"content": "original prompt"}},
        {
            "type": "assistant",
            "message": {
                "model": "<synthetic>",
                "content": [{"type": "text", "text": "Summary:\n\ncompaction"}],
            },
        },
        {"type": "ai-title", "aiTitle": "Wire up the SSE sidebar"},
    )
    assert transcript.title_from_lines(lines) == "Wire up the SSE sidebar"


def test_custom_title_beats_ai_title() -> None:
    lines = _lines(
        {"type": "ai-title", "aiTitle": "auto generated"},
        {"type": "custom-title", "customTitle": "My Renamed Session"},
    )
    assert transcript.title_from_lines(lines) == "My Renamed Session"


def test_skips_injected_system_user_and_non_typed() -> None:
    lines = _lines(
        {"type": "user", "message": {"content": "<system-reminder>noise</system-reminder>"}},
        {"type": "user", "promptSource": "tool_result", "message": {"content": "tool output"}},
        {"type": "user", "isMeta": True, "message": {"content": "meta"}},
    )
    assert transcript.title_from_lines(lines) is None


def test_ignores_non_summary_synthetic() -> None:
    lines = _lines(
        {
            "type": "assistant",
            "message": {
                "model": "<synthetic>",
                "content": [{"type": "text", "text": "No response requested."}],
            },
        },
    )
    assert transcript.title_from_lines(lines) is None


def test_empty_and_garbage() -> None:
    assert transcript.title_from_lines([]) is None
    assert transcript.title_from_lines(["", "not json", "{}"]) is None


def _codex_user(text: str) -> dict:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def test_codex_rollout_first_real_prompt() -> None:
    lines = _lines(
        {"type": "session_meta", "payload": {"id": "abc", "cwd": "/x"}},
        {"type": "event_msg", "payload": {"type": "task_started"}},
        # Codex injects its session context as the first user turn — skip it.
        _codex_user("<environment_context>\n  <cwd>/x</cwd>\n</environment_context>"),
        _codex_user("build me a thing"),
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "ok"}],
            },
        },
    )
    assert transcript.title_from_lines(lines) == "build me a thing"


def test_codex_rollout_no_user_prompt_is_none() -> None:
    lines = _lines(
        {"type": "session_meta", "payload": {"id": "abc"}},
        _codex_user("<environment_context>only injected context</environment_context>"),
    )
    assert transcript.title_from_lines(lines) is None


def test_title_from_file(tmp_path: Path) -> None:
    f = tmp_path / "t.jsonl"
    f.write_text(
        "\n".join(
            _lines(
                {"type": "user", "message": {"content": "hello from a file"}},
                {"type": "ai-title", "aiTitle": "Filed Title"},
            )
        )
        + "\n"
    )
    assert transcript.title_from_file(f) == "Filed Title"


def test_title_from_file_missing(tmp_path: Path) -> None:
    assert transcript.title_from_file(tmp_path / "nope.jsonl") is None


def test_title_from_file_tail_bytes(tmp_path: Path) -> None:
    # With a small tail window, the head-only first prompt is not seen, but a
    # near-the-end ai-title still is.
    f = tmp_path / "t.jsonl"
    head = json.dumps({"type": "user", "message": {"content": "x" * 5000}})
    tail = json.dumps({"type": "ai-title", "aiTitle": "Tail Title"})
    f.write_text(head + "\n" + tail + "\n")
    assert transcript.title_from_file(f, tail_bytes=200) == "Tail Title"
