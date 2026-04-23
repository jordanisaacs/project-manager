"""Terminal output helpers — ANSI color gated on TTY + standard env vars."""

from __future__ import annotations

import os
import sys

_COLORS = {
    "blue": "34",
    "green": "32",
    "yellow": "33",
    "magenta": "35",
    "cyan": "36",
    "red": "31",
}


def use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CLICOLOR_FORCE"):
        return True
    return sys.stdout.isatty()


def style(
    text: str,
    *,
    fg: str | None = None,
    bold: bool = False,
    dim: bool = False,
    strikethrough: bool = False,
) -> str:
    if not use_color():
        return text
    codes: list[str] = []
    if bold:
        codes.append("1")
    if dim:
        codes.append("2")
    if strikethrough:
        codes.append("9")
    if fg and fg in _COLORS:
        codes.append(_COLORS[fg])
    if not codes:
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"
