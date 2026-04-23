from __future__ import annotations

from typing import TYPE_CHECKING

from project_manager import output

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def short(commit: str) -> str:
    return commit[:12]


def style(
    text: str,
    *,
    fg: str | None = None,
    bold: bool = False,
    dim: bool = False,
    strikethrough: bool = False,
) -> str:
    return output.style(text, fg=fg, bold=bold, dim=dim, strikethrough=strikethrough)


def record(ctx: StackerCtx, logs: list[str], message: str) -> None:
    logs.append(message)
    if ctx.progress:
        ctx.progress(message)


def finish(ctx: StackerCtx, logs: list[str], final_message: str) -> str:
    if ctx.progress:
        return final_message
    return "\n".join([*logs, final_message]) if logs else final_message
