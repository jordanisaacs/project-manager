from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from project_manager.stacker import git
from project_manager.stacker.models import Details, Scope, SelectorTarget, TrackedBranch
from project_manager.stacker.ops.track import require_tracked
from project_manager.stacker.pr.lineage import lineage

from . import format as fmt
from . import graph

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


@dataclass(frozen=True)
class LsOptions:
    """Knobs for `ls_text`: scope selection + render detail + output format."""

    target_branch: str | None = None
    scope: Scope = "all"
    details: Details = "status-counts"
    json_output: bool = False


_DEFAULT_LS_OPTIONS = LsOptions()


def ls_text(
    ctx: StackerCtx,
    repo_name: str | None = None,
    options: LsOptions = _DEFAULT_LS_OPTIONS,
) -> str:
    """Render the stack tree with configurable detail level.

    `options.scope="current"` narrows the render to the lineage of
    `options.target_branch`; `scope="all"` shows every tracked branch
    grouped by repo. `options.details` controls per-branch annotations
    (none/status/status-counts/all). `options.json_output` emits a
    machine-readable tree instead of ANSI-styled text.
    """
    branches = _ls_branch_set(ctx, repo_name, options.target_branch, options.scope)
    if not branches:
        return "No tracked branches."
    if options.json_output:
        return _ls_json(ctx, branches, options.details)
    return _ls_tree(ctx, branches, options.details)


def _ls_branch_set(
    ctx: StackerCtx,
    repo_name: str | None,
    target_branch: str | None,
    scope: Scope,
) -> list[TrackedBranch]:
    if scope == "current":
        if repo_name is None or target_branch is None:
            raise git.GitError(
                "ls --scope current requires a repo and target branch."
            )
        target = require_tracked(
            ctx, SelectorTarget(repo_name=repo_name, branch=target_branch)
        )
        return lineage(ctx, target)
    return ctx.db.list_branches(repo_name)


def _ls_tree(
    ctx: StackerCtx, branches: list[TrackedBranch], details: Details
) -> str:
    by_repo: dict[str, list[TrackedBranch]] = {}
    for item in branches:
        by_repo.setdefault(item.repo_name, []).append(item)
    lines: list[str] = []
    for repo, items in sorted(by_repo.items()):
        repo_path = ctx.paths.repo(repo)
        try:
            live = {
                info.branch: info
                for info in git.worktree_list(repo_path)
                if info.branch
            }
        except git.GitError:
            live = {}
        lines.append(fmt.style(repo, fg="blue", bold=True))
        tracked_branches = {item.branch for item in items}
        roots: list[tuple[str, bool]] = []
        seen_roots: set[str] = set()
        for item in items:
            if (
                item.parent_branch not in tracked_branches
                and item.parent_branch not in seen_roots
            ):
                roots.append((item.parent_branch, True))
                seen_roots.add(item.parent_branch)
        gctx = graph.GraphCtx(repo_name=repo, items=items, live=live)
        for parent_branch, implicit in sorted(roots):
            graph.render_graph_node(
                ctx,
                lines,
                gctx,
                graph.GraphPos(
                    branch=parent_branch, prefix="", is_last=True, implicit=implicit,
                ),
                details=details,
            )
    return "\n".join(lines)


def _ls_json(
    ctx: StackerCtx, branches: list[TrackedBranch], details: Details
) -> str:
    """Machine-readable tree: one node per tracked branch.

    `details` controls which fields each node includes; `"none"`
    emits just names + parent links, fuller levels add op/sync
    state. Output is a flat list so consumers can rebuild the tree
    via parent_branch.
    """
    include_state = details in ("status", "status-counts", "all")
    include_counts = details in ("status-counts", "all")
    include_all = details == "all"
    entries: list[dict[str, object]] = []
    for item in branches:
        entry: dict[str, object] = {
            "repo_name": item.repo_name,
            "branch": item.branch,
            "parent_repo_name": item.parent_repo_name,
            "parent_branch": item.parent_branch,
        }
        if include_state:
            entry["synced"] = graph.is_synced(ctx, item)
        if include_counts:
            entry["managed_base"] = item.managed_base_commit
        if include_all:
            entry["last_synced_parent_commit"] = item.last_synced_parent_commit
            entry["last_clean_head"] = item.last_clean_head
            entry["pr_url"] = item.pr_url
        entries.append(entry)
    return json.dumps(entries, indent=2)
