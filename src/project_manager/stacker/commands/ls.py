"""`pm stacker ls`."""

from typing import Annotated, Literal

from cyclopts import Parameter

from project_manager import config, render
from project_manager.cli._shared import StackerScope
from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.project import current as project_current
from project_manager.project import discovery
from project_manager.stacker import git, locate
from project_manager.stacker.render.ls import LsOptions, RenderOptions

from . import _common, stacker_app

_DetailsLit = Literal["none", "status", "status-counts", "all"]
_MergedStyleLit = Literal["dimmed", "strikethrough", "normal"]
_ColorModeLit = Literal["off", "icon", "title", "full"]


def _resolve_current(paths: Paths) -> tuple[str, str] | None:
    """`(repo_name, branch)` of the cwd's pm slot, or None when cwd is outside.

    Used to render the `> ` / `(current)` marker in the ls tree.
    """
    slot = locate.slot_for_cwd(paths)
    if slot is None:
        return None
    try:
        branch = git.current_branch(slot.path)
    except git.GitError:
        return None
    if not branch:
        return None
    return slot.repo_name, branch


def _project_wt_labels(paths: Paths) -> dict[tuple[str, str], str]:
    """`{(repo, branch): wt_name}` for every live worktree in the cwd's project.

    Returns {} when cwd isn't inside a pm project OR the project db is
    missing. Lets the tree renderer replace the generic `(current)`
    marker with `(<wt-name>)` for every worktree the project owns —
    not just the single cwd slot — so running `pm stacker ls` from
    `~/.projects/<name>/` still labels sibling worktrees.
    """
    project = project_current.detect_current_project(paths)
    if project is None:
        return {}
    try:
        wt_rows = discovery.read_wts(paths, project)
    except ProjectError:
        return {}
    out: dict[tuple[str, str], str] = {}
    for wt, repo, slot_uuid in wt_rows:
        slot_path = paths.slot(repo, slot_uuid)
        if not slot_path.is_dir():
            continue
        try:
            branch = git.current_branch(slot_path)
        except git.GitError:
            continue
        if branch:
            out[(repo, branch)] = wt
    return out


@stacker_app.command
def ls(
    branch: str | None = None,
    scope: StackerScope = StackerScope(),
    *,
    current: Annotated[
        bool,
        Parameter(
            name=("-c", "--current"),
            negative="",
            help="narrow to the current branch's lineage (default: all)",
        ),
    ] = False,
    details: _DetailsLit = "status-counts",
    json: Annotated[bool, Parameter(negative="")] = False,
    icons: bool = True,
    merged: bool = True,
    merged_style: _MergedStyleLit = "dimmed",
    color_mode: _ColorModeLit = "icon",
    legend: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Render the stack tree."""
    paths = config.load()
    svc = _common.service(paths)
    current_pos = _resolve_current(paths)
    wt_labels = _project_wt_labels(paths)
    render_opts = RenderOptions(
        icons=icons,
        hide_merged=not merged,
        merged_style=merged_style,
        color_mode=color_mode,
    )
    if current:
        target = _common.target(scope.repo, branch, paths)
        result = svc.ls_text(
            target.repo_name,
            LsOptions(
                target_branch=target.branch,
                scope="current",
                details=details,
                json_output=json,
                current=current_pos,
                legend=legend,
                render=render_opts,
                wt_labels=wt_labels,
            ),
        )
    else:
        result = svc.ls_text(
            _common.resolve_repo_optional(scope.repo, paths),
            LsOptions(
                scope="all",
                details=details,
                json_output=json,
                current=current_pos,
                legend=legend,
                render=render_opts,
                wt_labels=wt_labels,
            ),
        )
    if json:
        render.emit_json_string(result)
    else:
        render.emit_markup(result)
    return 0
