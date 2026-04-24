"""Orchestration for `pm agent ls`: gather sources per project, render rows."""

import asyncio
from dataclasses import dataclass
from datetime import datetime

from project_manager import render
from project_manager.agent import scope
from project_manager.agent.sources import REGISTRY, SessionEntry
from project_manager.paths import Paths
from project_manager.render import Column, Section

_AGENT_STYLE: dict[str, str] = {
    "claude": "blue",
    "codex": "green",
    "cursor": "magenta",
}
_TITLE_DISPLAY = 60


@dataclass(frozen=True)
class AgentRow:
    """One row in `pm agent ls`'s sectioned table.

    `session_id` is the full vendor id (UUID today); the table keeps
    the full value so it can be pasted straight into a `--resume`
    invocation. `last_active` is UTC, rendered local.

    A row where every field except `project` is empty / None represents
    a placeholder emitted for pm projects that have zero sessions
    across every selected agent — the sectioned table will show the
    project name and a row of `-` markers so the user sees the project
    exists but has no chats. Without this, `pm agent ls --all` would
    silently drop projects, making it impossible to tell "no sessions"
    from "project doesn't exist".
    """

    project: str
    agent: str
    session_id: str
    title: str
    last_active: datetime | None

    def __pm_json__(self) -> dict[str, object]:
        # `datetime` has no default serializer in render._coerce; emit
        # ISO-8601 so machine consumers don't have to parse the display
        # format. Placeholder rows emit null so consumers can filter
        # them with `filter(r => r.last_active)` if wanted.
        return {
            "project": self.project,
            "agent": self.agent,
            "session_id": self.session_id,
            "title": self.title,
            "last_active": (
                self.last_active.isoformat()
                if self.last_active is not None else None
            ),
        }


def _placeholder(project: str) -> "AgentRow":
    """A sentinel row so an empty project still prints as a section.

    Column values render as empty strings, which `Column.empty` then
    replaces with the standard `-` placeholder in the table.
    """
    return AgentRow(
        project=project, agent="", session_id="", title="", last_active=None,
    )


def _truncate(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


COLUMNS: list[Column[AgentRow]] = [
    Column("Agent", "agent", style=lambda r: _AGENT_STYLE.get(r.agent, "")),
    # Full session id — lets the user copy-paste it straight into
    # `claude --resume <id>` / `codex resume <id>` / `cursor-agent
    # --resume <id>` without looking it up elsewhere.
    Column("Session", "session_id", style="dim"),
    Column("Title", lambda r: _truncate(r.title, _TITLE_DISPLAY)),
    Column(
        "Last Active",
        # Goes through `render.format_datetime` so every listing
        # command that shows a timestamp applies the same display-tz
        # rule (configured via `[display].timezone`).
        lambda r: (
            render.format_datetime(r.last_active)
            if r.last_active is not None else ""
        ),
    ),
]


def sections(rows: list[AgentRow]) -> list[Section[AgentRow]]:
    """Group rows by project, preserving the order of first appearance.

    The orchestrator produces rows in the order projects were requested
    (or discovered), so preserving insertion order here keeps the final
    table stable for the user.
    """
    by_project: dict[str, list[AgentRow]] = {}
    for row in rows:
        by_project.setdefault(row.project, []).append(row)
    return [Section(title=p, rows=rs) for p, rs in by_project.items()]


async def ls(
    paths: Paths,
    projects: list[str],
    limit: int,
    agents: frozenset[str],
) -> list[AgentRow]:
    """Fan out across `projects x selected agents` and merge to `AgentRow` list.

    Uses `asyncio.TaskGroup` at both levels so any source failure
    (missing schema, corrupt sqlite, bad JSON) propagates as an
    `ExceptionGroup` — we don't silently drop a source, since that would
    turn a real bug into a mysteriously-empty column. Sources still wrap
    their own blocking I/O in `asyncio.to_thread` for real parallelism.
    """
    if not projects:
        return []
    async with asyncio.TaskGroup() as tg:
        tasks = [
            tg.create_task(_fetch_project(paths, p, limit, agents))
            for p in projects
        ]
    return [row for t in tasks for row in t.result()]


async def _fetch_project(
    paths: Paths,
    project: str,
    limit: int,
    agents: frozenset[str],
) -> list[AgentRow]:
    owned = scope.owned_paths(paths, project)
    selected = [(name, fn) for name, fn in REGISTRY.items() if name in agents]
    if not selected:
        return []
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(fn(owned, limit)) for _, fn in selected]
    entries: list[SessionEntry] = [e for t in tasks for e in t.result()]
    if not entries:
        # Keep the project in the table so the user can tell "no
        # chats" apart from "project doesn't exist".
        return [_placeholder(project)]
    entries.sort(key=lambda e: e.last_active, reverse=True)
    top = entries[:limit]
    return [
        AgentRow(
            project=project,
            agent=e.agent,
            session_id=e.session_id,
            title=e.title,
            last_active=e.last_active,
        )
        for e in top
    ]
