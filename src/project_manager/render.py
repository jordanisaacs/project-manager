"""Unified CLI output: rich-backed tables, grouped sections, JSON, markup.

`Column` describes one output column against a domain dataclass; commands
fold their rows through `emit_rows` (flat) or `emit_sections` (grouped by a
title). Both paths share a JSON serializer — JSON dumps the raw dataclass
fields, not the display strings, so machine consumers get structured data.

Rich handles TTY vs pipe and the `NO_COLOR` / `FORCE_COLOR` env vars
natively via `Console`; there is no pm-specific color-gate shim.

Types can opt into a custom JSON shape by defining `__pm_json__(self) ->
dict`; otherwise the default serializer walks dataclass fields and coerces
`Enum` → `.value`, `Path` → `str`.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, tzinfo
from enum import Enum
from pathlib import Path
from typing import Generic, Literal, Protocol, TypeVar, cast, runtime_checkable

from rich import box
from rich.console import Console
from rich.style import Style
from rich.table import Table
from rich.text import Text

from project_manager import config as _config

# Time formatting lives here so every command rendering a timestamp
# hits the same code path — one place to own timezone conversion, one
# format per kind of thing being shown. The `_config` alias keeps the
# `config` name free for local `config` parameters in callers that
# wrap these helpers.

T = TypeVar("T")
_SELECTED_ROW_STYLE = Style(bgcolor="bright_white")
_SELECTED_TEXT_STYLE = Style(color="black")


@dataclass(frozen=True)
class Column(Generic[T]):
    """One output column: title, value accessor, optional per-row styling.

    Generic in the row type so callables keep their narrowed signatures
    (`(RepoRow) -> str`) without widening to `object`. `value` is either
    an attribute name on the row, or a callable deriving the display
    string. `style` is either a static rich style string, a callable
    `(row) -> style`, or None for unstyled. `empty` substitutes for
    None/empty cell values. `markup=True` parses rich markup inside the
    cell content — for pre-styled multi-line content (e.g. a captured
    stacker tree) where per-row `style` isn't enough.
    """

    title: str
    value: str | Callable[[T], object]
    style: str | Callable[[T], str | None] | None = None
    empty: str = "-"
    markup: bool = False

    def render_cell(self, row: T) -> str:
        raw = (
            getattr(row, self.value) if isinstance(self.value, str)
            else self.value(row)
        )
        if raw is None or raw == "":
            return self.empty
        return str(raw)

    def render_style(self, row: T) -> str | None:
        if self.style is None or isinstance(self.style, str):
            return self.style
        return self.style(row)

    def render_text(self, row: T) -> Text:
        raw = self.render_cell(row)
        if self.markup:
            return Text.from_markup(raw)
        return Text(raw, style=self.render_style(row) or "")


@runtime_checkable
class _PmJsonable(Protocol):
    def __pm_json__(self) -> dict[str, object]: ...


@dataclass(frozen=True)
class Section(Generic[T]):
    """One named group in a grouped-output command (e.g. one repo's slots).

    Rendered as a prepended group column in the shared table — the title
    appears only on the first row of the group, leaving the cell blank on
    the continuation rows so the value visually spans multiple rows.
    """

    title: str
    rows: Iterable[T] = ()


@dataclass(frozen=True)
class JsonShape:
    """Keys used to wrap a `Section` title + rows into a JSON object.

    Packaged as a struct so `emit_sections` stays within the project's
    arity budget without the CLI needing to pass both keys as loose
    kwargs. Default shape is `{"group": …, "items": […]}`; per-command
    overrides replace it with domain-specific names (e.g. `{"repo": …,
    "slots": […]}`).
    """

    group_key: str = "group"
    items_key: str = "items"


@dataclass(frozen=True)
class GroupColumn:
    """Display spec for the leading group column in `emit_sections`.

    `title` is the header text; `style` is the rich style applied to each
    non-empty cell. Bundled as a struct so CLIs pass one kwarg instead of
    two — keeps `emit_sections` within the project's arity budget.
    """

    title: str = "Group"
    style: str = "bold blue"


def console(*, stderr: bool = False) -> Console:
    """Construct a pm Console.

    Rich auto-detects TTY via `isatty()` and honors `NO_COLOR` /
    `FORCE_COLOR` natively. `soft_wrap=True` stops Rich from padding every
    row out to the full console width when rendering tables — tables size
    to their content instead. Repr highlighter and markup are off by
    default; callers opt into markup via `print(..., markup=True)`.
    """
    return Console(
        stderr=stderr, highlight=False, markup=False, soft_wrap=True,
    )


def emit_rows(
    rows: Iterable[T],
    columns: list[Column[T]],
    *,
    as_json: bool = False,
) -> None:
    """Flat table output. For single-row / ungrouped commands."""
    rows = list(rows)
    if as_json:
        console().print_json(data=[_default_to_dict(r) for r in rows])
        return
    _render_table(console(), rows, columns)


def emit_sections(
    sections: Iterable[Section[T]],
    columns: list[Column[T]],
    *,
    group: GroupColumn = GroupColumn(),
    as_json: bool = False,
    shape: JsonShape = JsonShape(),
) -> None:
    """Grouped table output — one table with a leading group column.

    The group value renders only on the first row of its group; the cell
    stays blank on continuation rows, so the label visually spans the
    group's rows. All other columns align across the whole table.

    JSON shape: `[{shape.group_key: title, shape.items_key: [raw_row...]}]`.
    """
    materialized: list[tuple[str, list[T]]] = [
        (s.title, list(s.rows)) for s in sections
    ]
    if as_json:
        console().print_json(data=[
            {
                shape.group_key: title,
                shape.items_key: [_default_to_dict(r) for r in rows],
            }
            for title, rows in materialized
        ])
        return
    if not materialized:
        return
    t = build_sections_table(materialized, columns, group=group)
    _emit_table_capture(console(), t, indent="")


def build_sections_table(
    materialized: list[tuple[str, list[T]]],
    columns: list[Column[T]],
    *,
    group: GroupColumn = GroupColumn(),
    selected_flat_idx: int | None = None,
) -> Table:
    """Build the `Table` that `emit_sections` would print, without printing.

    Factored so the interactive picker (`tui.pick`) can feed a freshly
    built Table into `rich.live.Live` on every keystroke. `emit_sections`
    composes this, so there is one renderer for the grouped table.

    Takes sections pre-materialized as `(title, rows)` tuples (the form
    `emit_sections` already builds internally) so this function stays
    cheap to call in a per-keystroke loop — no re-iteration of the
    caller's sections.

    `selected_flat_idx` is a 0-based index over the flattened row stream
    (section-order, row-order within section). The matched row is drawn
    as a cursor bar: one shared row background, with selected text
    recolored on top of it. While a selection is active we also collapse
    adjacent cell padding so the highlight fills the inter-column gaps
    instead of looking like separate highlighted cells. `None` (the
    default) draws no highlight, matching `emit_sections`' original
    output byte-for-byte.
    """
    flat_rows = [r for _, rs in materialized for r in rs]
    other_widths = _compute_widths(flat_rows, columns)
    group_width = max(
        len(group.title),
        *(len(title) for title, _ in materialized),
    ) if materialized else len(group.title)
    t = Table(
        box=box.HORIZONTALS, show_edge=False, pad_edge=False, padding=(0, 2),
        collapse_padding=selected_flat_idx is not None,
        show_header=True, header_style="dim",
    )
    t.add_column(group.title, min_width=group_width)
    for i, col in enumerate(columns):
        t.add_column(col.title, min_width=other_widths[i])
    last_section_idx = len(materialized) - 1
    flat_idx = 0
    for section_idx, (title, rows) in enumerate(materialized):
        is_group_boundary_section = section_idx < last_section_idx
        if not rows:
            # Empty group still emits a row so the title is visible (e.g.
            # a project with no attached worktrees). Synthetic rows aren't
            # selectable, so flat_idx isn't bumped — keeps the picker's
            # flat_rows derivation in tui.pick aligned with this index.
            t.add_row(
                Text(title, style=group.style),
                *(Text("") for _ in columns),
                end_section=is_group_boundary_section,
            )
            continue
        last_row_idx = len(rows) - 1
        for i, r in enumerate(rows):
            # end_section on the last row of each group (except the final
            # group) draws a horizontal rule between groups, matching the
            # header rule emitted by `box.HORIZONTALS`.
            is_group_boundary = i == last_row_idx and is_group_boundary_section
            is_selected = flat_idx == selected_flat_idx
            t.add_row(
                Text(
                    title if i == 0 else "",
                    style=_selected_style(group.style) if is_selected
                    else group.style,
                ),
                *(
                    Text(
                        col.render_cell(r),
                        style=_selected_style(col.render_style(r))
                        if is_selected else (col.render_style(r) or ""),
                    )
                    for col in columns
                ),
                end_section=is_group_boundary,
                style=_SELECTED_ROW_STYLE if is_selected else None,
            )
            flat_idx += 1
    return t


def _selected_style(base: str | None) -> Style:
    """Map a cell's normal style to the selected-row foreground style."""
    parsed = Style.parse(base) if base else Style()
    return parsed + _SELECTED_TEXT_STYLE


def emit_json(payload: object) -> None:
    """Pretty JSON (syntax-highlighted on TTY, plain on pipe)."""
    console().print_json(data=payload)


def emit_json_string(s: str) -> None:
    """Pretty-print a pre-serialized JSON string (re-parsed for highlighting)."""
    console().print_json(s)


def emit_markup(s: str) -> None:
    """Print a rich-markup string (markup resolved; auto-stripped on pipe)."""
    console().print(s, markup=True, highlight=False)


@dataclass(frozen=True)
class SubprocessLogLine:
    """One line of subprocess output, tagged with when and where it came from.

    Built by `subprocess_run.run` for every line read from a child's stdout
    or stderr; fed back here for formatting. Fields are the raw ingredients
    of the prefix, not a pre-formatted string, so every rendering path
    (live emit, captured error message, future log file) shares one
    formatter.
    """

    timestamp: datetime
    pid: int
    cmd: str
    stream: Literal["out", "err"]
    text: str


def emit_subprocess_line(line: SubprocessLogLine) -> None:
    """Print one tagged subprocess line to stderr, live.

    Used for `stream=True` subprocesses so the user watches output as the
    child emits it, with per-line attribution for parallel/concurrent runs.
    Always stderr — subprocess output must never pollute our stdout, which
    can be carrying JSON.
    """
    console(stderr=True).print(_subprocess_line_text(line), highlight=False)


def emit_command_start(cmd: list[str]) -> None:
    """Print a `[HH:MM:SS.mmm start] <cmd>` line to stderr before a subprocess runs.

    Announces work at the point of dispatch, not at the point of first
    output — so users see that their `pm repo pull` / `pm repo maintenance`
    actually kicked off a git child, even when the child is silent on
    success. Uses the same time format as the per-line subprocess prefix
    so start and output lines line up visually when they interleave.
    """
    ts = format_log_time(datetime.now(tz=UTC))
    t = Text()
    t.append(f"[{ts} ", style="dim")
    t.append("start", style="cyan")
    t.append("] ", style="dim")
    t.append(" ".join(cmd))
    console(stderr=True).print(t, highlight=False)


def format_subprocess_lines(lines: Iterable[SubprocessLogLine]) -> str:
    """Render a sequence of tagged lines into a single string.

    Used by `format_command_failure` to build the `CommandError` message.
    Rendered through a stderr `Console` so ANSI styling appears only when
    stderr is a TTY.
    """
    c = console(stderr=True)
    with c.capture() as cap:
        for line in lines:
            c.print(_subprocess_line_text(line), highlight=False)
    return cap.get().rstrip("\n")


_DATETIME_FMT = "%Y-%m-%d %H:%M"
_LOG_TIME_FMT = "%H:%M:%S"


def format_datetime(dt: datetime) -> str:
    """`YYYY-MM-DD HH:MM` in the configured display timezone.

    The shared path for "when did this happen?" columns in listing
    commands (session lists, etc.). Minute precision is the right
    granularity for human-scan tables — seconds add noise; the exact
    instant is preserved in JSON output via `isoformat()`.
    """
    return dt.astimezone(_display_tz()).strftime(_DATETIME_FMT)


def format_log_time(dt: datetime) -> str:
    """`HH:MM:SS.mmm` in the configured display timezone.

    Used for per-line subprocess log prefixes where sub-second ordering
    matters (interleaved stdout/stderr from parallel children). Only
    the time of day is shown — the date is implicit in "this run".
    """
    base = dt.astimezone(_display_tz()).strftime(_LOG_TIME_FMT)
    return f"{base}.{dt.microsecond // 1000:03d}"


def _display_tz() -> tzinfo | None:
    """Fetch the user-configured display tz, or None for system local.

    `config.display()` re-reads the TOML each call — negligible given
    how few timestamps a listing command prints, and useful because
    tests set their own PM_CONFIG via monkeypatch (a cached return
    would leak across test cases).
    """
    return _config.display().timezone


def _subprocess_line_text(line: SubprocessLogLine) -> Text:
    """Build the styled `rich.text.Text` for one tagged subprocess line.

    Styling: prefix is dim/bold so the eye skips past it; `[out]` is green
    and `[err]` is yellow so a wall of output still shows errors at a
    glance. No markup parsing — subprocess output can contain `[...]`
    safely.
    """
    ts = format_log_time(line.timestamp)
    stream_style = "green" if line.stream == "out" else "yellow"
    t = Text()
    t.append(f"[{ts} ", style="dim")
    t.append(f"pid={line.pid} ", style="dim")
    t.append(line.cmd, style="italic dim")
    t.append("] ", style="dim")
    t.append(f"[{line.stream}] ", style=stream_style)
    t.append(line.text)
    return t


def _render_table(
    c: Console,
    rows: list[T],
    columns: list[Column[T]],
) -> None:
    t = Table(
        box=box.HORIZONTALS, show_edge=False, pad_edge=False, padding=(0, 2),
        show_header=True, header_style="dim",
    )
    for col in columns:
        t.add_column(col.title)
    for r in rows:
        t.add_row(*(col.render_text(r) for col in columns))
    _emit_table_capture(c, t, indent="")


def _emit_table_capture(c: Console, t: Table, *, indent: str) -> None:
    """Print a rich Table through a capture buffer with per-line rstrip.

    Rich's default print pads every row out to the console width, which
    leaves noisy trailing whitespace on pipes. Capture the rendered output
    once, rstrip per line, then re-emit — gives clean, diffable output
    without losing ANSI sequences (the trailing chars are whitespace, not
    reset codes).
    """
    with c.capture() as cap:
        c.print(t)
    for line in cap.get().rstrip("\n").split("\n"):
        stripped = line.rstrip()
        c.print(indent + stripped if stripped else "")


def _compute_widths(rows: list[T], columns: list[Column[T]]) -> list[int]:
    """Max display width per column across all rows + the column header.

    Consulted by `emit_sections` to share widths across per-section
    sub-tables. Uses the rendered string length (post-`empty` substitution)
    so the returned widths match what each `_render_table` will emit.
    """
    widths: list[int] = []
    for col in columns:
        width = len(col.title)
        for r in rows:
            width = max(width, len(col.render_cell(r)))
        widths.append(width)
    return widths


def _default_to_dict(row: object) -> dict[str, object]:
    """Serialize a row for JSON output.

    Protocol: types can opt into a custom shape by defining `__pm_json__`.
    Otherwise we walk dataclass fields and coerce `Enum` → `.value` and
    `Path` → `str` so the JSON is consumable without follow-up parsing.
    """
    if isinstance(row, _PmJsonable):
        return row.__pm_json__()
    if is_dataclass(row) and not isinstance(row, type):
        return {f.name: _coerce(getattr(row, f.name)) for f in fields(row)}
    if isinstance(row, dict):
        return {str(k): _coerce(v) for k, v in cast("dict[object, object]", row).items()}
    raise TypeError(f"cannot serialize {type(row).__name__} to dict")


def _coerce(value: object) -> object:
    """JSON-friendly coercion: unwrap Enum, Path, nested dataclasses, sequences."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _coerce(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_coerce(v) for v in value]
    if is_dataclass(value) and not isinstance(value, type):
        return _default_to_dict(value)
    return value
