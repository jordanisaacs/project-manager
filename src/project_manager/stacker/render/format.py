from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape as _escape

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def short(commit: str) -> str:
    return commit[:12]


def escape(text: str) -> str:
    """Escape `[...]` so user-supplied strings can't break rich markup."""
    return _escape(text)


def style(
    text: str,
    *,
    fg: str | None = None,
    bold: bool = False,
    dim: bool = False,
    strikethrough: bool = False,
) -> str:
    """Wrap leaf text in rich markup.

    Auto-escapes `[`/`]` in `text` so literals like `[LOCAL]` / `[dirty]`
    and user-supplied URLs inside brackets can't collide with rich tags.
    For wrapping already-styled content (nested markup), build the tag
    directly: `f"[dim]{already_styled}[/]"`.
    """
    tokens: list[str] = []
    if bold:
        tokens.append("bold")
    if dim:
        tokens.append("dim")
    if strikethrough:
        tokens.append("strike")
    if fg:
        tokens.append(fg)
    safe = _escape(text)
    if not tokens:
        return safe
    return f"[{' '.join(tokens)}]{safe}[/]"


def record(ctx: StackerCtx, logs: list[str], message: str) -> None:
    logs.append(message)
    if ctx.progress:
        ctx.progress(message)


def finish(ctx: StackerCtx, logs: list[str], final_message: str) -> str:
    if ctx.progress:
        return final_message
    return "\n".join([*logs, final_message]) if logs else final_message
