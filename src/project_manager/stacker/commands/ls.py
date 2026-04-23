"""`pm stacker ls`."""
from typing import Annotated, Literal

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import StackerScope
from project_manager.paths import Paths
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
) -> int:
    """Render the stack tree."""
    paths = config.load()
    svc = _common.service(paths)
    current_pos = _resolve_current(paths)
    render_opts = RenderOptions(
        icons=icons,
        hide_merged=not merged,
        merged_style=merged_style,
        color_mode=_common.resolve_color_mode(color_mode),
    )
    if current:
        target = _common.target(scope.repo, branch, paths)
        return _common.emit(
            svc.ls_text(
                target.repo_name,
                LsOptions(
                    target_branch=target.branch,
                    scope="current",
                    details=details,
                    json_output=json,
                    current=current_pos,
                    render=render_opts,
                ),
            )
        )
    return _common.emit(
        svc.ls_text(
            _common.resolve_repo_optional(scope.repo, paths),
            LsOptions(
                scope="all",
                details=details,
                json_output=json,
                current=current_pos,
                render=render_opts,
            ),
        )
    )
