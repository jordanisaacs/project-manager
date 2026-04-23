from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from project_manager.stacker import git, locate, selectors
from project_manager.stacker.models import Details, TrackedBranch

from . import format as fmt

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


@dataclass(frozen=True)
class GraphCtx:
    """Per-repo state for graph rendering; threaded through recursion."""

    repo_name: str
    items: list[TrackedBranch]
    live: dict[str, git.WorktreeInfo]


@dataclass(frozen=True)
class GraphPos:
    """Branch identity + position of its node in the rendered tree."""

    branch: str
    prefix: str
    is_last: bool
    implicit: bool


def render_graph_node(
    ctx: StackerCtx,
    lines: list[str],
    gctx: GraphCtx,
    pos: GraphPos,
    *,
    details: Details = "status-counts",
) -> None:
    label = selectors.selector_for(gctx.repo_name, pos.branch)
    branch_label = fmt.style(label, fg="green", bold=not pos.implicit)
    suffixes = _node_suffixes(ctx, gctx, pos, details)
    connector = "└── " if pos.is_last else "├── "
    line = f"{pos.prefix}{connector}{branch_label}"
    if suffixes:
        line = f"{line} {' '.join(suffixes)}"
    lines.append(line)

    children = sorted(
        (item for item in gctx.items if item.parent_branch == pos.branch),
        key=lambda item: item.branch,
    )
    child_prefix = pos.prefix + ("    " if pos.is_last else "│   ")
    for index, child in enumerate(children):
        render_graph_node(
            ctx,
            lines,
            gctx,
            GraphPos(
                branch=child.branch,
                prefix=child_prefix,
                is_last=index == len(children) - 1,
                implicit=False,
            ),
            details=details,
        )


def _node_suffixes(
    ctx: StackerCtx,
    gctx: GraphCtx,
    pos: GraphPos,
    details: Details,
) -> list[str]:
    if details == "none":
        return []
    suffixes: list[str] = []
    tracked = next((item for item in gctx.items if item.branch == pos.branch), None)
    if pos.implicit:
        suffixes.append(fmt.style("[untracked root]", fg="yellow"))
        return suffixes
    if tracked is None:
        return suffixes
    synced = is_synced(ctx, tracked)
    suffixes.append(
        fmt.style(
            "[synced]" if synced else "[unsynced]",
            fg="cyan" if synced else "yellow",
        )
    )
    if pos.branch in gctx.live and has_graph_blocking_changes(gctx.live[pos.branch].path):
        suffixes.append(fmt.style("[dirty]", fg="red", bold=True))
    if details in ("status-counts", "all"):
        count = count_branch_commits(ctx, tracked)
        if count is not None:
            suffixes.append(fmt.style(f"[{count} commits]", fg="white"))
    if details == "all" and tracked.pr_url:
        suffixes.append(fmt.style(f"[{tracked.pr_url}]", fg="magenta"))
    return suffixes


def count_branch_commits(ctx: StackerCtx, tracked: TrackedBranch) -> int | None:
    path = locate.locate_worktree(ctx.paths, tracked.repo_name, tracked.branch)
    if path is None:
        return None
    with contextlib.suppress(git.GitError):
        return git.rev_count(path, f"{tracked.managed_base_commit}..HEAD")
    return None


def is_synced(ctx: StackerCtx, tracked: TrackedBranch) -> bool:
    try:
        parent_head = git.rev_parse(
            ctx.paths.repo(tracked.parent_repo_name), tracked.parent_branch
        )
    except git.GitError:
        return False
    return parent_head == tracked.managed_base_commit


def has_graph_blocking_changes(path: Path) -> bool:
    with contextlib.suppress(git.GitError):
        return git.has_tracked_changes(path)
    return False


def ensure_syncable(path: Path) -> None:
    if git.has_tracked_changes(path):
        raise git.GitError(
            f"Tracked changes present in {path}. Commit or discard them before syncing."
        )
    if git.cherry_pick_in_progress(path):
        raise git.GitError(
            f"Cherry-pick already in progress in {path}. Resolve it before starting "
            "another sync."
        )
