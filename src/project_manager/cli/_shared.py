"""Cyclopts root App plus cross-cutting helpers for `pm` commands.

- `root` is the top-level cyclopts App every group registers against.
- `WtSelection` / `ProjectOrAllScope` / `StackerScope` are flattened
  parameter dataclasses reused across many commands (see cyclopts
  "Sharing Parameters"). The bare `--project` / `--repo` building blocks
  these compose against live in `project_manager.cli._params`.
- `fail` renders the consistent `pm: <msg>` error line used everywhere.
"""
import sys
from dataclasses import dataclass
from typing import Annotated

from cyclopts import App, Parameter

from project_manager.cli._params import (
    ProjectArg,
    ProjectFlag,
    RepoArg,
    RepoFlag,
)
from project_manager.paths import Paths
from project_manager.project import current, discovery

root = App(
    name="pm",
    help="Project manager: pool of git worktrees bound to named projects via symlinks.",
)


def fail(msg: str) -> int:
    """Print `pm: <msg>` to stderr, return exit code 2."""
    print(f"pm: {msg}", file=sys.stderr)
    return 2


# --- reusable scope / selection dataclasses ----------------------------------


@Parameter(name="*")
@dataclass(frozen=True)
class WtSelection:
    """`--wt <a,b>` or `--all`; exactly one must be set (enforced in body)."""

    wt: Annotated[
        str | None, Parameter(help="comma-separated worktree names"),
    ] = None
    all: Annotated[
        bool, Parameter(name="--all", negative="", help="every worktree in the project"),
    ] = False


def selected_wts(sel: WtSelection) -> list[str] | None:
    """Translate `WtSelection` into the `list[str] | None` convention impls use."""
    if sel.all and sel.wt:
        raise ValueError("pass either --wt or --all, not both")
    if sel.all:
        return None
    if not sel.wt:
        raise ValueError("pass --wt <names> or --all")
    return [w.strip() for w in sel.wt.split(",") if w.strip()]


@Parameter(name="*")
@dataclass(frozen=True)
class ProjectOrAllScope:
    """`-p/--project <name>` or `--all`; mutually exclusive.

    With neither set, the resolver defers to `detect_current_project`
    and falls back to every project when the cwd is not inside one —
    so commands work both from inside a project (single-project view)
    and from anywhere else (all-projects view).
    """

    project: ProjectArg = None
    all: Annotated[
        bool,
        Parameter(name="--all", negative="", help="every project"),
    ] = False


def resolve_project_scope(paths: Paths, scope: ProjectOrAllScope) -> list[str]:
    """Turn a `ProjectOrAllScope` into a concrete list of project names."""
    if scope.project is not None and scope.all:
        raise ValueError("pass either --project or --all, not both")
    if scope.project is not None:
        # Validate existence so a typo fails fast instead of returning [].
        discovery.require_project_db(paths, scope.project)
        return [scope.project]
    if scope.all:
        return [name for name, _ in discovery.list_project_dbs(paths)]
    detected = current.detect_current_project(paths)
    if detected is not None:
        return [detected]
    return [name for name, _ in discovery.list_project_dbs(paths)]


@Parameter(name="*")
@dataclass(frozen=True)
class StackerScope:
    """`--repo / -a / --skip-* / --from` walk flags.

    `branch` stays a function-level positional arg so existing
    `pm stacker <cmd> [<branch>]` UX is preserved.
    """

    repo: RepoArg = None
    all: Annotated[
        bool,
        Parameter(
            name=("-a", "--all"),
            negative="",
            help="every tracked branch in the repo (default: current lineage)",
        ),
    ] = False
    skip_ancestors: Annotated[
        bool,
        Parameter(negative="", help="exclude ancestors from the walk"),
    ] = False
    skip_descendants: Annotated[
        bool,
        Parameter(negative="", help="exclude descendants from the walk"),
    ] = False
    from_branch: Annotated[
        str | None,
        Parameter(name="--from", help="start the walk from this branch"),
    ] = None


__all__ = [
    "ProjectFlag",
    "ProjectOrAllScope",
    "RepoFlag",
    "StackerScope",
    "WtSelection",
    "fail",
    "resolve_project_scope",
    "root",
    "selected_wts",
]
