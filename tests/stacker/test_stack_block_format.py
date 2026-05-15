"""Compose/strip contract for the managed block.

Pure-string tests over `pr/find.py` boundary helpers; no fixtures needed.

The managed block is wrapped in `<!-- stacker:begin -->` / `<!-- stacker:end -->`
HTML-comment markers (our authoritative parsing boundary), with the
gitstack-format header + separator emitted *inside* the markers so
ReviewStack's gitstack parser recognizes the PR as stacked.
"""

from __future__ import annotations

from project_manager.stacker.pr.find import (
    compose_body_with_block,
    strip_managed_block,
)
from project_manager.stacker.pr.stack_block import STACK_HEADER, STACK_SEPARATOR


def _block(branches: str = "- [feat](https://github.com/o/r/pull/1)") -> str:
    return (
        "<!-- stacker:begin -->\n"
        f"{STACK_HEADER}\n"
        "\n"
        f"{branches}\n"
        "\n"
        f"{STACK_SEPARATOR}\n"
        "<!-- stacker:end -->"
    )


def test_strip_managed_block_removes_markered_section() -> None:
    body = f"{_block()}\n\nUser content here\n\n## Summary"
    assert strip_managed_block(body) == "User content here\n\n## Summary"


def test_strip_managed_block_no_markers_returns_body_stripped() -> None:
    body = "  no managed block in here  "
    assert strip_managed_block(body) == "no managed block in here"


def test_strip_managed_block_ignores_lone_gitstack_header_in_user_text() -> None:
    # User wrote `## 🥞 Stacked PR` and `---------` in their own template
    # but did not wrap them in our HTML markers — strip must leave them.
    body = "## 🥞 Stacked PR (mentioned by user)\n---------\nUser body"
    assert strip_managed_block(body) == body.strip()


def test_compose_body_with_block_empty_block_returns_user_body() -> None:
    assert compose_body_with_block("Hello", "") == "Hello"


def test_compose_body_with_block_emits_block_then_user_content() -> None:
    block = _block()
    out = compose_body_with_block("User text", block)
    assert out == f"{block}\n\nUser text"


def test_compose_body_with_block_strips_existing_managed_block() -> None:
    # Body already has a stale managed block — compose must replace, not duplicate.
    stale_body = f"{_block()}\n\nUser text"
    fresh_block = _block("- [feat](https://github.com/o/r/pull/2)")
    out = compose_body_with_block(stale_body, fresh_block)
    # Exactly one begin marker, one end marker.
    assert out.count("<!-- stacker:begin -->") == 1
    assert out.count("<!-- stacker:end -->") == 1
    # Fresh block content survived; stale did not.
    assert "/pull/2" in out
    assert "/pull/1" not in out
    # User content survived.
    assert "User text" in out


def test_composed_body_satisfies_gitstack_parser_signature() -> None:
    """ReviewStack's gitstack parser keys off the inner header + separator."""
    block = _block()
    out = compose_body_with_block("## Summary\n\nThe details", block)
    # gitstackParser.ts:179 — header substring check.
    assert STACK_HEADER in out
    # gitstackParser.ts:184 — separator presence check.
    assert any(line.strip().startswith(STACK_SEPARATOR) for line in out.split("\n"))
    # The separator must precede the user content (parser slices commit
    # message from after the separator).
    sep_line = next(
        i for i, line in enumerate(out.split("\n")) if line.strip().startswith(STACK_SEPARATOR)
    )
    summary_line = next(
        i for i, line in enumerate(out.split("\n")) if line.strip() == "## Summary"
    )
    assert summary_line > sep_line
