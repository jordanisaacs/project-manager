"""Reusable terminal-UI components. One public function today: `pick`.

The picker does not render its own table — it composes `render.build_sections_
table` on every keystroke and hands the result to `rich.live.Live`. Any command
that already builds `Section[T]` + `Column[T]` for `emit_sections` can offer an
interactive variant with the same look by calling `pick` instead.

Key handling is intentionally small: up/down movement (arrow / vim / emacs),
Enter to select, Esc / q / Ctrl-C to cancel. The `_read_key_stdin` default is
swapped for a scripted callable in tests via the `read_key` parameter on
`_pick_core`, so the navigation loop is exercisable without touching termios.
"""

import os
import select
import sys
import termios
import tty
from collections.abc import Callable, Iterable
from enum import Enum, auto
from typing import TypeVar

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.text import Text

from project_manager import render
from project_manager.render import Column, GroupColumn, Section

__all__ = ["Key", "TuiError", "pick"]

T = TypeVar("T")


class TuiError(Exception):
    """Raised when the terminal isn't interactive enough to drive a TUI."""


class Key(Enum):
    """Decoded key event. Unrecognized keys collapse to `OTHER`."""

    UP = auto()
    DOWN = auto()
    ENTER = auto()
    CANCEL = auto()
    OTHER = auto()


# After a lone `\x1b`, wait this long for a CSI follow-up byte. A bare Esc
# press has no follow-up; an arrow key sends `\x1b[A/B` within microseconds.
# 50ms is well above the follow-up latency and well below human reaction time,
# so it reliably tells the two apart without feeling sluggish on Esc.
_ESC_PEEK_TIMEOUT = 0.05

_HELP = "↑/↓ · j/k · ^P/^N · Enter to select · Esc to cancel"

# Single-byte key → Key mapping. Table lookup keeps `_read_key_stdin`
# a flat decode (esc-peek + CSI parse + lookup) with only the escape
# branch as control flow.
_SINGLE_BYTE_KEYS: dict[bytes, Key] = {
    b"\r": Key.ENTER, b"\n": Key.ENTER,
    b"k": Key.UP,     b"\x10": Key.UP,   # k / Ctrl-P
    b"j": Key.DOWN,   b"\x0e": Key.DOWN, # j / Ctrl-N
    b"q": Key.CANCEL, b"\x03": Key.CANCEL,  # q / Ctrl-C byte
}


def _read_key_stdin() -> Key:
    """Default key reader: decode one key event from stdin's raw fd.

    Assumes `pick` has already put the tty in cbreak mode. Reads through
    `os.read` (not `sys.stdin.read`) to bypass Python's line buffer, which
    would otherwise swallow single keypresses until a newline.
    """
    fd = sys.stdin.fileno()
    b = os.read(fd, 1)
    if b == b"\x1b":
        # CSI sequences (`\x1b[A`, `\x1b[B`) start with Esc; a bare Esc
        # press has no follow-up. Peek the fd briefly to disambiguate.
        ready, _, _ = select.select([fd], [], [], _ESC_PEEK_TIMEOUT)
        if not ready:
            return Key.CANCEL
        rest = os.read(fd, 2)
        if rest == b"[A":
            return Key.UP
        if rest == b"[B":
            return Key.DOWN
        return Key.OTHER
    return _SINGLE_BYTE_KEYS.get(b, Key.OTHER)


def pick(
    sections: Iterable[Section[T]],
    columns: list[Column[T]],
    *,
    group: GroupColumn = GroupColumn(),
    header: str | None = None,
    is_selectable: Callable[[T], bool] = lambda _: True,
) -> T | None:
    """Interactive row selector over a grouped table.

    Renders the exact same table `emit_sections` would produce, with one
    row highlighted as the current selection. Arrow / `j`/`k` / `^N`/`^P`
    keys move the highlight; Enter returns the selected row; Esc / `q` /
    Ctrl-C returns `None`.

    `is_selectable(row)` controls which rows the highlight is allowed to
    land on — placeholder / separator rows can still render (for visual
    continuity with the non-interactive view) while being skipped by
    navigation. If no row is selectable, returns `None` immediately
    without entering the event loop.

    Raises `TuiError` if stdin is not a TTY. The call site should
    ideally pre-check this and surface a friendlier command-level error
    message first; this library-level guard is a safety net.
    """
    materialized = [(s.title, list(s.rows)) for s in sections]
    flat_rows: list[T] = [r for _, rs in materialized for r in rs]
    selectable_idxs = [
        i for i, r in enumerate(flat_rows) if is_selectable(r)
    ]
    if not selectable_idxs:
        return None
    if not sys.stdin.isatty():
        raise TuiError("interactive picker requires a TTY on stdin")
    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    try:
        # cbreak (not raw) leaves ISIG on so Ctrl-C still delivers SIGINT
        # → KeyboardInterrupt, which we catch below as a cancel.
        tty.setcbreak(fd)
        return _pick_core(
            materialized, flat_rows, selectable_idxs, columns,
            group=group, header=header,
            read_key=_read_key_stdin, console=Console(stderr=True),
        )
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


def _pick_core(  # noqa: PLR0913  — 8 args but all narrowly typed + keyword-only
    materialized: list[tuple[str, list[T]]],
    flat_rows: list[T],
    selectable_idxs: list[int],
    columns: list[Column[T]],
    *,
    group: GroupColumn,
    header: str | None,
    read_key: Callable[[], Key],
    console: Console,
) -> T | None:
    """Navigation + render loop, split from `pick` as a test seam.

    No termios, no TTY check — the caller owns those so tests can exercise
    the movement / selection logic with a scripted `read_key` and a
    non-terminal `console`.
    """
    cur = 0  # index into selectable_idxs

    def frame() -> RenderableType:
        table = render.build_sections_table(
            materialized, columns, group=group,
            selected_flat_idx=selectable_idxs[cur],
        )
        parts: list[RenderableType] = []
        if header is not None:
            parts.append(Text(header, style="bold"))
            parts.append(Text(""))
        parts.append(table)
        parts.append(Text(""))
        parts.append(Text(_HELP, style="dim"))
        return Group(*parts)

    # transient=True wipes the picker on exit so the shell scrollback stays
    # clean whether we cancel or exec into an agent immediately after.
    with Live(
        frame(), console=console, auto_refresh=False, transient=True,
    ) as live:
        while True:
            try:
                key = read_key()
            except KeyboardInterrupt:
                return None
            if key is Key.CANCEL:
                return None
            if key is Key.ENTER:
                return flat_rows[selectable_idxs[cur]]
            changed = False
            if key is Key.UP and cur > 0:
                cur -= 1
                changed = True
            elif key is Key.DOWN and cur < len(selectable_idxs) - 1:
                cur += 1
                changed = True
            if changed:
                live.update(frame(), refresh=True)
