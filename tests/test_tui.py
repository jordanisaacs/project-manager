"""Tests for `project_manager.tui`.

The picker's navigation/selection loop is exercised through `_pick_core`
with a scripted `read_key` and a non-terminal `Console` — so these tests
never touch termios or real stdin and don't depend on running in a TTY.
`pick`'s TTY guard is exercised directly (it raises before touching
termios).
"""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from rich.console import Console

from project_manager import tui
from project_manager.render import Column, GroupColumn, Section
from project_manager.tui import Key, TuiError


@dataclass(frozen=True)
class _Row:
    name: str
    selectable: bool = True


_COLUMNS: list[Column[_Row]] = [Column("Name", "name")]


def _script(keys: list[Key]) -> Callable[[], Key]:
    """Return a `read_key` callable that yields `keys` in order.

    After the last key the script raises — a well-behaved picker must
    have returned by then. Surfacing it as a loud test failure prevents
    silent infinite loops.
    """
    it = iter(keys)

    def read() -> Key:
        try:
            return next(it)
        except StopIteration as e:
            raise AssertionError("picker read past end of key script") from e

    return read


def _sink_console() -> Console:
    # Non-terminal destination: rich's Live becomes a near-no-op (no cursor
    # hiding, no alt-screen), so tests don't need a real TTY.
    return Console(file=io.StringIO(), force_terminal=False, width=80)


def _call_core(
    sections: list[Section[_Row]],
    keys: list[Key],
    *,
    is_selectable: Callable[[_Row], bool] = lambda _: True,
) -> _Row | None:
    materialized = [(s.title, list(s.rows)) for s in sections]
    flat_rows = [r for _, rs in materialized for r in rs]
    selectable_idxs = [i for i, r in enumerate(flat_rows) if is_selectable(r)]
    return tui._pick_core(
        materialized,
        flat_rows,
        selectable_idxs,
        _COLUMNS,
        group=GroupColumn("G"),
        header=None,
        read_key=_script(keys),
        console=_sink_console(),
    )


def test_enter_on_first_row_returns_first_row() -> None:
    secs = [Section(title="s", rows=[_Row("a"), _Row("b"), _Row("c")])]
    assert _call_core(secs, [Key.ENTER]) == _Row("a")


def test_down_down_enter_returns_third_row() -> None:
    secs = [Section(title="s", rows=[_Row("a"), _Row("b"), _Row("c")])]
    assert _call_core(secs, [Key.DOWN, Key.DOWN, Key.ENTER]) == _Row("c")


def test_down_up_enter_returns_first_row() -> None:
    secs = [Section(title="s", rows=[_Row("a"), _Row("b"), _Row("c")])]
    assert _call_core(secs, [Key.DOWN, Key.UP, Key.ENTER]) == _Row("a")


def test_up_at_top_stays_at_top() -> None:
    secs = [Section(title="s", rows=[_Row("a"), _Row("b")])]
    # Extra UPs at the top should have no effect; next ENTER returns row 0.
    assert _call_core(secs, [Key.UP, Key.UP, Key.UP, Key.ENTER]) == _Row("a")


def test_down_at_bottom_stays_at_bottom() -> None:
    secs = [Section(title="s", rows=[_Row("a"), _Row("b")])]
    assert _call_core(
        secs,
        [Key.DOWN, Key.DOWN, Key.DOWN, Key.ENTER],
    ) == _Row("b")


def test_cancel_returns_none() -> None:
    secs = [Section(title="s", rows=[_Row("a"), _Row("b")])]
    assert _call_core(secs, [Key.CANCEL]) is None


def test_other_keys_are_ignored() -> None:
    secs = [Section(title="s", rows=[_Row("a"), _Row("b")])]
    # OTHER should not move the cursor or select — only the final ENTER
    # resolves the pick, still on row 0.
    assert _call_core(
        secs,
        [Key.OTHER, Key.OTHER, Key.ENTER],
    ) == _Row("a")


def test_keyboard_interrupt_cancels() -> None:
    """Ctrl-C during cbreak raises KeyboardInterrupt in the caller's
    key reader; the picker treats it as cancel, not an unhandled crash."""

    def read() -> Key:
        raise KeyboardInterrupt

    materialized = [("s", [_Row("a")])]
    got = tui._pick_core(
        materialized,
        [_Row("a")],
        [0],
        _COLUMNS,
        group=GroupColumn("G"),
        header=None,
        read_key=read,
        console=_sink_console(),
    )
    assert got is None


def test_is_selectable_skips_placeholder_rows() -> None:
    """Placeholder rows still render (visual continuity with
    `pm agent ls`) but navigation hops over them — DOWN from row 0
    lands on row 2, skipping the middle placeholder."""
    secs = [
        Section(
            title="s",
            rows=[
                _Row("a", selectable=True),
                _Row("placeholder", selectable=False),
                _Row("c", selectable=True),
            ],
        ),
    ]
    got = _call_core(
        secs,
        [Key.DOWN, Key.ENTER],
        is_selectable=lambda r: r.selectable,
    )
    assert got == _Row("c")


def test_all_unselectable_returns_none_without_entering_loop() -> None:
    """No row is selectable → picker returns None immediately; the
    scripted reader is never consumed, which our `_script` would
    flag as an assertion error if the picker tried to read."""
    secs = [
        Section(
            title="s",
            rows=[
                _Row("p1", selectable=False),
                _Row("p2", selectable=False),
            ],
        ),
    ]
    got = tui.pick(
        secs,
        _COLUMNS,
        group=GroupColumn("G"),
        is_selectable=lambda r: r.selectable,
    )
    assert got is None


def test_empty_sections_returns_none() -> None:
    got = tui.pick([], _COLUMNS, group=GroupColumn("G"))
    assert got is None


def test_pick_raises_tui_error_on_non_tty_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Library-level guard — call sites should normally pre-check, but
    `pick` still fails loudly rather than silently hanging on a non-TTY."""

    class NonTtyStdin:
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr("sys.stdin", NonTtyStdin())
    secs = [Section(title="s", rows=[_Row("a")])]
    with pytest.raises(TuiError, match="TTY"):
        tui.pick(secs, _COLUMNS, group=GroupColumn("G"))
