"""Tests for the render helper.

Uses pytest's `monkeypatch` to replace `render.console()` with one that
writes to a StringIO, with either `force_terminal=True` (to assert on
ANSI-styled output) or `force_terminal=False` (to assert on pipe-shaped
plain output).
"""
from __future__ import annotations

import io
import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import pytest
from rich.console import Console

from project_manager import render
from project_manager.render import Column, JsonShape, Section


@dataclass(frozen=True)
class _Row:
    name: str
    count: int


def _capture(
    monkeypatch: pytest.MonkeyPatch,
    fn: Callable[[], None],
    *,
    tty: bool,
) -> str:
    """Run `fn()` with `render.console()` patched to return a captured Console."""
    buf = io.StringIO()

    def make_console(*, stderr: bool = False) -> Console:
        _ = stderr
        return Console(
            file=buf,
            force_terminal=tty,
            color_system="truecolor" if tty else None,
            no_color=not tty,
            highlight=False,
            markup=False,
            soft_wrap=True,
            width=80,
        )

    monkeypatch.setattr(render, "console", make_console)
    fn()
    return buf.getvalue()


_COLUMNS: list[Column[_Row]] = [
    Column("Name", "name", style="blue"),
    Column("Count", "count", style=lambda r: "red" if r.count == 0 else "green"),
]


def _data_lines(out: str) -> list[list[str]]:
    """Split table output into tokens per row, dropping separator (`─`) lines."""
    return [
        line.split()
        for line in out.rstrip().split("\n")
        if line and not line.strip().startswith("─")
    ]


def test_emit_rows_pipe_is_plain_tabular_no_ansi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [_Row("foo", 3), _Row("bar", 0)]
    out = _capture(monkeypatch, lambda: render.emit_rows(rows, _COLUMNS), tty=False)
    assert "\x1b[" not in out
    # Header separator rule between header and data.
    assert "─" in out
    tokens = _data_lines(out)
    assert tokens == [["Name", "Count"], ["foo", "3"], ["bar", "0"]]


def test_emit_rows_tty_emits_ansi_per_column_style(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [_Row("foo", 3), _Row("bar", 0)]
    out = _capture(monkeypatch, lambda: render.emit_rows(rows, _COLUMNS), tty=True)
    assert "\x1b[" in out
    assert "\x1b[34m" in out  # blue Name column
    assert "\x1b[32m" in out  # green count>0
    assert "\x1b[31m" in out  # red count==0


def test_emit_rows_json_emits_raw_dataclass_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [_Row("foo", 3), _Row("bar", 0)]
    out = _capture(
        monkeypatch,
        lambda: render.emit_rows(rows, _COLUMNS, as_json=True),
        tty=False,
    )
    payload = json.loads(out)
    assert payload == [
        {"name": "foo", "count": 3},
        {"name": "bar", "count": 0},
    ]


def test_emit_rows_json_respects_pm_json_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass(frozen=True)
    class Custom:
        a: int
        b: int
        def __pm_json__(self) -> dict[str, object]:
            return {"sum": self.a + self.b}

    custom_cols: list[Column[Custom]] = []
    out = _capture(
        monkeypatch,
        lambda: render.emit_rows([Custom(2, 5)], custom_cols, as_json=True),
        tty=False,
    )
    assert json.loads(out) == [{"sum": 7}]


def test_default_to_dict_coerces_enum_and_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Color(StrEnum):
        RED = "red"

    @dataclass(frozen=True)
    class Row:
        color: Color
        path: Path

    row_cols: list[Column[Row]] = []
    out = _capture(
        monkeypatch,
        lambda: render.emit_rows(
            [Row(Color.RED, Path("/nonexistent/x"))], row_cols, as_json=True,
        ),
        tty=False,
    )
    payload = json.loads(out)
    assert payload == [{"color": "red", "path": "/nonexistent/x"}]


def test_emit_sections_renders_single_table_with_group_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The group value shows only on the first row of each group; all
    other columns align across the whole table. Horizontal rules appear
    under the header and between groups.
    """
    secs = [
        Section(title="first", rows=[_Row("a", 1), _Row("b", 2)]),
        Section(title="second", rows=[_Row("c", 3)]),
    ]
    out = _capture(
        monkeypatch,
        lambda: render.emit_sections(
            secs, _COLUMNS, group=render.GroupColumn("G"),
        ),
        tty=False,
    )
    # Two rules: one under the header, one between the two sections.
    rule_lines = [
        line for line in out.split("\n") if line and line.strip().startswith("─")
    ]
    assert len(rule_lines) == 2
    assert _data_lines(out) == [
        ["G", "Name", "Count"],
        ["first", "a", "1"],
        # Continuation row: group cell blank (drops out of split tokens).
        ["b", "2"],
        ["second", "c", "3"],
    ]
    assert out.count("Name") == 1  # header appears exactly once


def test_emit_sections_json_uses_jsonshape_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secs = [
        Section(title="runtime", rows=[_Row("a", 1)]),
        Section(title="universe", rows=[_Row("b", 2)]),
    ]
    out = _capture(
        monkeypatch,
        lambda: render.emit_sections(
            secs, _COLUMNS, as_json=True, shape=JsonShape("repo", "slots"),
        ),
        tty=False,
    )
    payload = json.loads(out)
    assert payload == [
        {"repo": "runtime",  "slots": [{"name": "a", "count": 1}]},
        {"repo": "universe", "slots": [{"name": "b", "count": 2}]},
    ]


def test_emit_sections_default_jsonshape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secs = [Section(title="x", rows=[_Row("a", 1)])]
    out = _capture(
        monkeypatch,
        lambda: render.emit_sections(secs, _COLUMNS, as_json=True),
        tty=False,
    )
    assert json.loads(out) == [
        {"group": "x", "items": [{"name": "a", "count": 1}]},
    ]


def test_emit_markup_resolves_rich_tags_on_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _capture(monkeypatch, lambda: render.emit_markup("[green]hi[/]"), tty=True)
    assert "\x1b[32m" in out
    assert "hi" in out
    assert "[green]" not in out
    assert "[/]" not in out


def test_emit_markup_strips_tags_on_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _capture(monkeypatch, lambda: render.emit_markup("[green]hi[/]"), tty=False)
    assert "\x1b[" not in out
    assert "hi" in out.strip()
    assert "[green]" not in out


def test_emit_markup_preserves_literal_brackets_in_user_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Branch suffix tokens like `[LOCAL]` / `[MERGED]` are literal text,
    not markup — rich's parser leaves unknown identifiers alone.
    """
    out = _capture(
        monkeypatch,
        lambda: render.emit_markup("[yellow][LOCAL][/]"),
        tty=False,
    )
    assert "[LOCAL]" in out


def test_column_empty_substitution_on_none_and_empty_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass(frozen=True)
    class R:
        v: str | None

    cols: list[Column[R]] = [Column("V", "v", empty="(none)")]
    out = _capture(
        monkeypatch,
        lambda: render.emit_rows([R(None), R(""), R("x")], cols),
        tty=False,
    )
    assert _data_lines(out) == [["V"], ["(none)"], ["(none)"], ["x"]]


def test_column_callable_value_and_callable_style(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass(frozen=True)
    class R:
        n: int

    cols: list[Column[R]] = [
        Column(
            "N",
            lambda r: f"#{r.n}",
            style=lambda r: "red" if r.n < 0 else "green",
        ),
    ]
    out = _capture(
        monkeypatch,
        lambda: render.emit_rows([R(1), R(-1)], cols),
        tty=True,
    )
    assert "#1" in out
    assert "#-1" in out
    assert "\x1b[32m" in out
    assert "\x1b[31m" in out


def test_default_to_dict_raises_on_non_dataclass_non_dict() -> None:
    with pytest.raises(TypeError, match="cannot serialize"):
        render._default_to_dict(42)
