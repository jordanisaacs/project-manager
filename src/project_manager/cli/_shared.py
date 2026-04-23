"""Cyclopts root App plus cross-cutting helpers for `pm` commands.

- `root` is the top-level cyclopts App every group registers against.
- `RepoBranch` / `WtSelection` / `StackerScope` are flattened parameter
  dataclasses reused across many commands (see cyclopts "Sharing
  Parameters").
- `fail` renders the consistent `pm: <msg>` error line used everywhere.
"""
import sys
from dataclasses import dataclass
from typing import Annotated

from cyclopts import App, Parameter

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
class ProjectFlag:
    """`-p/--project <name>`; defaults to the cwd's current project."""

    project: Annotated[
        str | None,
        Parameter(
            name=("-p", "--project"),
            help="project name (defaults to current project)",
        ),
    ] = None


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
class RepoFlag:
    """Just `--repo`; defaults to the cwd's pm slot."""

    repo: Annotated[
        str | None, Parameter(help="defaults to the cwd's pm slot"),
    ] = None


@Parameter(name="*")
@dataclass(frozen=True)
class StackerScope:
    """`--repo / -a / --skip-* / --from` walk flags.

    `branch` stays a function-level positional arg so existing
    `pm stacker <cmd> [<branch>]` UX is preserved.
    """

    repo: Annotated[
        str | None, Parameter(help="defaults to the cwd's pm slot"),
    ] = None
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
