"""Tests for the render helper.

Uses pytest's `monkeypatch` to replace `render.console()` with one that
writes to a StringIO, with either `force_terminal=True` (to assert on
ANSI-styled output) or `force_terminal=False` (to assert on pipe-shaped
plain output).
"""
from __future__ import annotations

import io
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

import pytest
from rich.console import Console
from rich.style import Style
from rich.text import Text

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
        Section(title="backend", rows=[_Row("a", 1)]),
        Section(title="frontend", rows=[_Row("b", 2)]),
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
        {"repo": "backend",  "slots": [{"name": "a", "count": 1}]},
        {"repo": "frontend", "slots": [{"name": "b", "count": 2}]},
    ]


def test_build_sections_table_is_unstyled_by_default() -> None:
    """`build_sections_table` is the pure Table-builder behind
    `emit_sections`; with no `selected_flat_idx` every row has
    `style=None`, matching what the non-interactive listing has always
    shown."""
    secs = [
        Section(title="first", rows=[_Row("a", 1), _Row("b", 2)]),
        Section(title="second", rows=[_Row("c", 3)]),
    ]
    materialized = [(s.title, list(s.rows)) for s in secs]
    table = render.build_sections_table(
        materialized, _COLUMNS, group=render.GroupColumn("G"),
    )
    # Row count: 2 + 1 data rows. Header is a separate attribute, not a row.
    assert len(table.rows) == 3
    assert table.collapse_padding is False
    assert all(r.style is None for r in table.rows)


def test_build_sections_table_marks_selected_row_reverse() -> None:
    """With `selected_flat_idx` set, only that row gets the cursor-bar
    styling and the table collapses adjacent cell padding."""
    secs = [
        Section(title="first", rows=[_Row("a", 1), _Row("b", 2)]),
        Section(title="second", rows=[_Row("c", 3)]),
    ]
    materialized = [(s.title, list(s.rows)) for s in secs]
    table = render.build_sections_table(
        materialized, _COLUMNS,
        group=render.GroupColumn("G"), selected_flat_idx=2,
    )
    styles = [r.style for r in table.rows]
    # Flat order: ("first","a"), ("first","b"), ("second","c") — index 2
    # is the "c" row in the second section.
    assert table.collapse_padding is True
    assert styles == [None, None, render._SELECTED_ROW_STYLE]
    cells = [table.columns[i]._cells[2] for i in range(3)]
    assert all(isinstance(c, Text) for c in cells)
    assert cast("Text", cells[0]).style == Style.parse("bold black")
    assert cast("Text", cells[1]).style == Style.parse("black")
    assert cast("Text", cells[2]).style == Style.parse("green black")


def test_selected_row_uses_background_bar_not_reverse_video() -> None:
    secs = [Section(title="first", rows=[_Row("a", 1)])]
    materialized = [(s.title, list(s.rows)) for s in secs]
    table = render.build_sections_table(
        materialized, _COLUMNS,
        group=render.GroupColumn("G"), selected_flat_idx=0,
    )
    console = Console(
        file=io.StringIO(),
        force_terminal=True,
        color_system="standard",
        highlight=False,
        markup=False,
        soft_wrap=True,
        width=80,
        record=True,
    )
    console.print(table)
    out = console.export_text(styles=True)
    assert re.search(r"\x1b\[[0-9;]*\b7m", out) is None
    assert "107m" in out


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


# --- time formatting helpers ---


def _write_tz_config(tmp_path: Path, tz_name: str) -> Path:
    cfg = tmp_path / "pm.toml"
    cfg.write_text(
        '[paths]\nrepos = "/r"\nworktrees = "/w"\nprojects = "/p"\n'
        'stacker_root = "/s"\n'
        f'[display]\ntimezone = "{tz_name}"\n',
    )
    return cfg


def test_format_datetime_applies_configured_timezone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Every listing command rendering a time goes through
    `render.format_datetime` so `[display].timezone` is honored in one
    place — not re-implemented per handler.
    """
    from datetime import UTC, datetime
    monkeypatch.setenv("PM_CONFIG", str(_write_tz_config(tmp_path, "America/Los_Angeles")))
    # 2026-04-24 07:00 UTC -> 2026-04-24 00:00 PDT (-7h DST offset).
    dt = datetime(2026, 4, 24, 7, 0, tzinfo=UTC)
    assert render.format_datetime(dt) == "2026-04-24 00:00"


def test_format_log_time_applies_configured_timezone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from datetime import UTC, datetime
    monkeypatch.setenv("PM_CONFIG", str(_write_tz_config(tmp_path, "America/Los_Angeles")))
    dt = datetime(2026, 4, 24, 7, 0, 45, 123456, tzinfo=UTC)
    # Same wall clock math + ms precision preserved for interleaved
    # subprocess-line ordering.
    assert render.format_log_time(dt) == "00:00:45.123"


def test_format_datetime_falls_back_to_system_tz_when_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from datetime import UTC, datetime
    monkeypatch.delenv("PM_CONFIG", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    # Only asserting it doesn't crash and produces a plausible string
    # — system tz varies between CI runners, so exact match is brittle.
    dt = datetime(2026, 4, 24, 7, 0, tzinfo=UTC)
    out = render.format_datetime(dt)
    assert len(out) == len("2026-04-24 07:00")
