"""`pm project status`."""
import asyncio
from typing import Annotated

from cyclopts import Parameter

from project_manager import check as check_mod
from project_manager import config, render
from project_manager.agent import ls as agent_ls
from project_manager.agent.sources import REGISTRY
from project_manager.project import current
from project_manager.project import status as status_mod
from project_manager.project.status import ALL_STATUS_SECTIONS, StatusSection

from . import project_app

_SESSIONS_LIMIT = 15


def _parse_sections(raw: str | None) -> frozenset[StatusSection]:
    """Parse `worktrees,prs,...` into a `StatusSection` set.

    `None` (the absent flag) means "everything"; a typo like
    `pm project status -s wokrtrees` raises `ValueError`, which the
    top-level handler renders as the standard `pm: <msg>` line.
    """
    if raw is None:
        return ALL_STATUS_SECTIONS
    pieces = [p.strip() for p in raw.split(",") if p.strip()]
    if not pieces:
        raise ValueError("--section value is empty")
    valid = {s.value for s in StatusSection}
    out: set[StatusSection] = set()
    for p in pieces:
        if p not in valid:
            raise ValueError(
                f"unknown section {p!r} — pick from "
                f"{', '.join(sorted(valid))}",
            )
        out.add(StatusSection(p))
    return frozenset(out)


def _emit_json(
    ps: status_mod.ProjectStatus,
    sessions_rows: list,
    selected: frozenset[StatusSection],
) -> None:
    """Build and emit the section-keyed JSON payload.

    Only emits keys for requested sections so consumers can tell
    "section was skipped" from "section was empty".
    """
    payload: dict[str, object] = {}
    if StatusSection.WORKTREES in selected:
        payload["worktrees"] = [r.__pm_json__() for r in ps.worktrees]
    if StatusSection.PRS in selected:
        payload["prs"] = [r.__pm_json__() for r in ps.prs]
    if StatusSection.STACKER in selected:
        payload["stacker"] = [r.__pm_json__() for r in ps.stacker]
    if StatusSection.SESSIONS in selected:
        payload["sessions"] = [r.__pm_json__() for r in sessions_rows]
    render.emit_json(payload)


def _emit_text(
    ps: status_mod.ProjectStatus,
    sessions_rows: list,
    selected: frozenset[StatusSection],
) -> None:
    """Print one heading + table per requested section, in stable order."""
    first = True

    def heading(name: str) -> str:
        nonlocal first
        prefix = "" if first else "\n"
        first = False
        return f"{prefix}[bold]{name}[/bold]"

    if StatusSection.WORKTREES in selected:
        render.emit_markup(heading("Worktrees"))
        render.emit_sections(
            status_mod.worktree_sections(ps.worktrees),
            status_mod.WORKTREE_COLUMNS,
            group=render.GroupColumn("Repo"),
        )
    if StatusSection.PRS in selected and ps.prs:
        render.emit_markup(heading("PRs"))
        render.emit_rows(ps.prs, status_mod.PR_COLUMNS)
    if StatusSection.STACKER in selected and ps.stacker:
        render.emit_markup(heading("Stacker"))
        render.emit_rows(ps.stacker, status_mod.STACKER_COLUMNS)
    if StatusSection.SESSIONS in selected:
        render.emit_markup(heading("Recent sessions"))
        render.emit_sections(
            agent_ls.sections(sessions_rows),
            agent_ls.COLUMNS,
            group=render.GroupColumn("Project"),
        )


@project_app.command
def status(
    project: str | None = None,
    *,
    json: Annotated[bool, Parameter(negative="")] = False,
    sections: Annotated[
        str | None,
        Parameter(
            name=("-s", "--section"),
            help="comma-separated subset: worktrees,prs,stacker,sessions",
        ),
    ] = None,
) -> int:
    """Show worktree health, PR state, stacker arms, and recent agent sessions.

    With no `--section`, every section is gathered and printed (the
    historical default). Pass `-s worktrees` (or any comma-separated
    subset) to skip the work for the others — useful for partial
    refreshes from external front-ends and for cheaper monitoring
    queries when only one slice matters.
    """
    paths = config.load()
    resolved = current.resolve_project(paths, project)
    selected = _parse_sections(sections)
    ps = status_mod.status(paths, resolved, selected)
    sessions_rows = (
        asyncio.run(
            agent_ls.ls(paths, [resolved], _SESSIONS_LIMIT, frozenset(REGISTRY)),
        )
        if StatusSection.SESSIONS in selected else []
    )
    if json:
        _emit_json(ps, sessions_rows, selected)
    else:
        _emit_text(ps, sessions_rows, selected)
    # Exit code reflects worktree health only when worktrees were asked
    # for — a sessions-only call shouldn't fail the shell pipeline just
    # because some unrelated worktree is in DRIFT.
    if StatusSection.WORKTREES in selected:
        non_healthy = [
            r for r in ps.worktrees if r.finding.kind != check_mod.Kind.ACTIVE
        ]
        return 1 if non_healthy else 0
    return 0
