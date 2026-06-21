from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from project_manager import config
from project_manager.pool.db import OwnerKind, PoolDB
from project_manager.stacker import git
from project_manager.stacker.models import (
    ColorMode,
    Details,
    MergedStyle,
    PRState,
    Scope,
    SelectorTarget,
    TrackedBranch,
)
from project_manager.stacker.ops.track import require_tracked
from project_manager.stacker.pr.lineage import lineage
from project_manager.stacker.pr.refresh import refresh_review_state

from . import format as fmt
from . import graph
from . import prefetch as prefetch_mod

if TYPE_CHECKING:
    from pathlib import Path

    from project_manager.paths import Paths
    from project_manager.stacker.ctx import StackerCtx


@dataclass(frozen=True)
class RenderOptions:
    """Visual knobs for the text renderer.

    Grows across stages — icons/color-mode/merged-style are scaffolded
    here in Stage 1, applied in full in Stage 2. `online` is flipped on
    in Stage 5 when bulk GraphQL fetching lands.
    """

    icons: bool = True
    hide_merged: bool = False
    merged_style: MergedStyle = "dimmed"
    color_mode: ColorMode = "icon"
    online: bool = True


@dataclass(frozen=True)
class LsOptions:
    """Knobs for `ls_text`: scope selection + render detail + output format."""

    target_branch: str | None = None
    scope: Scope = "all"
    details: Details = "status-counts"
    json_output: bool = False
    # (repo_name, branch) of the branch checked out in the cwd's pm slot,
    # or None when cwd is not inside a slot. Used to mark the current row
    # with `> ` / `(current)`.
    current: tuple[str, str] | None = None
    # Prepend a legend explaining icons, commit groups, and suffix tokens.
    # Ignored in JSON mode — JSON consumers don't need glyph docs.
    legend: bool = False
    render: RenderOptions = field(default_factory=RenderOptions)
    # Optional per-branch label that replaces the generic `(current)`
    # marker with `(<label>)`. Keyed on `(repo_name, branch)` so the
    # same-branch-name-in-different-repos case can't misfire. Populated
    # by `pm project status` and by `pm stacker ls` when cwd resolves
    # to a pm project — a project may have multiple worktrees (one per
    # branch), and the generic `(current)` marker only fits the single
    # cwd branch.
    wt_labels: dict[tuple[str, str], str] | None = None


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
    if options.render.online:
        refresh_review_state(ctx, branches)
    if options.render.hide_merged:
        merged = {(pr.repo_name, pr.branch) for pr in ctx.db.list_pr_states(repo_name) if pr.merged}
        branches = [b for b in branches if (b.repo_name, b.branch) not in merged]
    if options.json_output:
        # `--legend` is intentionally ignored here — JSON consumers don't
        # need glyph docs, and injecting them would break the parse.
        return _ls_json(ctx, branches, options.details, options.current)
    body = (
        _empty_text(ctx, repo_name, options.current)
        if not branches
        else _ls_tree(ctx, branches, options)
    )
    if options.legend:
        return body + "\n\n" + graph.render_legend(options.render)
    return body


# Pool worktrees are laid out as `<paths.worktrees>/<repo>/<uuid>/...`.
_WORKTREE_MIN_PARTS = 2


def _resolve_branch_projects(
    paths: Paths,
    live: dict[str, git.WorktreeInfo],
) -> dict[str, str]:
    """Map each live branch to the pm project whose slot has it checked out.

    Walks the repo's live worktrees, matches paths under `paths.worktrees`,
    and looks up the slot owner in the pool db. Branches checked out in
    non-pm worktrees, in STACKER-owned slots, or in unclaimed slots are
    omitted.
    """
    if not live:
        return {}
    try:
        pool_root = paths.worktrees.resolve()
    except FileNotFoundError:
        return {}
    pooldb = PoolDB(paths.pool_db())
    out: dict[str, str] = {}
    for branch, info in live.items():
        project = _project_for_path(pooldb, pool_root, info.path)
        if project is not None:
            out[branch] = project
    return out


def _project_for_path(
    pooldb: PoolDB,
    pool_root: Path,
    wt_path: Path,
) -> str | None:
    try:
        rel = wt_path.resolve().relative_to(pool_root)
    except (FileNotFoundError, ValueError):
        return None
    parts = rel.parts
    if len(parts) < _WORKTREE_MIN_PARTS:
        return None
    owner = pooldb.get_owner(parts[0], parts[1])
    if owner is None or owner.kind != OwnerKind.PROJECT:
        return None
    return owner.id


def _empty_text(
    ctx: StackerCtx,
    repo_name: str | None,
    current: tuple[str, str] | None,
) -> str:
    """Empty-set message. Adds the "not tracked" note when it applies.

    A tracked current branch implies the result set cannot truly be
    empty, so we only need to surface the unmanaged-branch case here.
    """
    if current is None:
        return "No tracked branches."
    cur_repo, cur_branch = current
    if repo_name is not None and cur_repo != repo_name:
        return "No tracked branches."
    if ctx.db.get_branch(cur_repo, cur_branch) is not None:
        return "No tracked branches."
    return f"No tracked branches in {cur_repo}. Current branch `{cur_branch}` is not tracked by pm."


def _ls_branch_set(
    ctx: StackerCtx,
    repo_name: str | None,
    target_branch: str | None,
    scope: Scope,
) -> list[TrackedBranch]:
    if scope == "current":
        if repo_name is None or target_branch is None:
            raise git.GitError("ls --scope current requires a repo and target branch.")
        target = require_tracked(ctx, SelectorTarget(repo_name=repo_name, branch=target_branch))
        return lineage(ctx, target)
    return ctx.db.list_branches(repo_name)


def _ls_tree(
    ctx: StackerCtx,
    branches: list[TrackedBranch],
    options: LsOptions,
) -> str:
    details = options.details
    current = options.current
    render_opts = options.render
    wt_labels = options.wt_labels
    by_repo: dict[str, list[TrackedBranch]] = {}
    for item in branches:
        by_repo.setdefault(item.repo_name, []).append(item)
    # One concurrent pre-pass computes every per-repo and per-node git
    # fact before rendering. The render walk below is pure dict lookups.
    facts = prefetch_mod.prefetch_all(
        ctx,
        by_repo,
        details=details,
        current=current,
        limit=config.concurrency().limit,
    )
    lines: list[str] = []
    for repo, items in sorted(by_repo.items()):
        repo_facts = facts[repo]
        live = repo_facts.live
        pr_states = repo_facts.pr_states
        branch_projects = repo_facts.branch_projects
        # Two-space gutter mirrors the per-row `> ` current marker.
        lines.append("  " + fmt.style(repo, fg="blue", bold=True))
        tracked_branches = {item.branch for item in items}
        if current is not None and current[0] == repo and current[1] not in tracked_branches:
            # Tracked branches already signal their state via the `> / (current)`
            # marker; only call out the non-managed case, where the marker
            # can't fire and the reader would otherwise miss the branch.
            lines.append(
                "  "
                + fmt.style(
                    f"(current branch `{current[1]}` is not tracked by pm)",
                    fg="yellow",
                ),
            )
        roots: list[tuple[str, bool]] = []
        seen_roots: set[str] = set()
        for item in items:
            if item.parent_branch not in tracked_branches and item.parent_branch not in seen_roots:
                roots.append((item.parent_branch, True))
                seen_roots.add(item.parent_branch)
        current_branch = current[1] if current and current[0] == repo else None
        gctx = graph.GraphCtx(
            repo_name=repo,
            items=items,
            live=live,
            details=details,
            render_opts=render_opts,
            pr_states=pr_states,
            current_branch=current_branch,
            branch_projects=branch_projects,
            wt_labels=wt_labels or {},
            node_status=repo_facts.node_status,
        )
        for parent_branch, implicit in sorted(roots):
            graph.render_graph_node(
                ctx,
                lines,
                gctx,
                graph.GraphPos(
                    branch=parent_branch,
                    prefix="",
                    is_last=True,
                    implicit=implicit,
                ),
            )
    return "\n".join(lines)


_STATUS_JSON: dict[str, str] = {
    "local_only": "local_only",
    "no_pr": "no_pr",
    "pr_open": "pr_open",
    "merged": "merged",
    "pr_draft": "pr_draft",
    "pr_approved": "pr_approved",
    "pr_open_comments": "pr_open_comments",
    "pr_approved_comments": "pr_approved_comments",
}


def ls_structure(
    ctx: StackerCtx,
    branches: list[TrackedBranch],
    details: Details,
    current: tuple[str, str] | None,
) -> dict[str, object]:
    """Hierarchical structure: `{current_branch, branches: [root...with children]}`.

    Tiered fields mirror `--details`: `none` only structural fields,
    `status` adds status/needs_sync/ahead_of_remote, `status-counts`
    adds commit_count, `all` surfaces cached git SHAs for debugging.
    """
    include_state = details in ("status", "status-counts", "all")
    include_counts = details in ("status-counts", "all")
    include_all = details == "all"
    pr_map: dict[tuple[str, str], PRState] = {
        (pr.repo_name, pr.branch): pr for pr in ctx.db.list_pr_states()
    }
    tracked_keys: set[tuple[str, str]] = {(b.repo_name, b.branch) for b in branches}
    children_map: dict[tuple[str, str], list[TrackedBranch]] = {}
    for b in branches:
        children_map.setdefault((b.parent_repo_name, b.parent_branch), []).append(b)
    by_repo: dict[str, list[TrackedBranch]] = {}
    for b in branches:
        by_repo.setdefault(b.repo_name, []).append(b)
    # One async prefetch feeds both the tree and JSON paths. JSON output
    # reflects cached state only (matching gitstack's emit-don't-fetch
    # semantics), so RenderOptions isn't threaded through here.
    facts = prefetch_mod.prefetch_all(
        ctx,
        by_repo,
        details=details,
        current=current,
        limit=config.concurrency().limit,
    )

    def node(b: TrackedBranch) -> dict[str, object]:
        pr = pr_map.get((b.repo_name, b.branch))
        status = facts[b.repo_name].node_status.get(b.branch)
        if status is None:
            status = graph.NodeStatus(
                synced=False,
                commit_count=0,
                ahead_of_remote=0,
                has_upstream=False,
                dirty=False,
                is_current=False,
                merged=pr is not None and pr.merged,
            )
        entry: dict[str, object] = {
            "repo_name": b.repo_name,
            "branch": b.branch,
            "parent_repo_name": b.parent_repo_name,
            "parent_branch": b.parent_branch,
            "is_root": (b.parent_repo_name, b.parent_branch) not in tracked_keys,
            "pr_url": pr.pr_url if pr is not None else None,
            "merged": pr.merged if pr is not None else False,
            "status": _STATUS_JSON[graph.classify(b, pr, status, online=False)]
            if include_state
            else None,
            "needs_sync": (not status.synced) if include_state else None,
            "ahead_of_remote": status.ahead_of_remote if include_counts else None,
            "commit_count": status.commit_count if include_counts else None,
        }
        if include_all:
            entry["managed_base"] = b.managed_base_commit
            entry["last_synced_parent_commit"] = b.last_synced_parent_commit
            entry["last_clean_head"] = b.last_clean_head
        kids = sorted(
            children_map.get((b.repo_name, b.branch), []),
            key=lambda c: c.branch,
        )
        entry["children"] = [node(k) for k in kids]
        return entry

    roots = sorted(
        [b for b in branches if (b.parent_repo_name, b.parent_branch) not in tracked_keys],
        key=lambda b: (b.repo_name, b.branch),
    )
    return {
        "current_branch": current[1] if current is not None else None,
        "branches": [node(r) for r in roots],
    }


def _ls_json(
    ctx: StackerCtx,
    branches: list[TrackedBranch],
    details: Details,
    current: tuple[str, str] | None,
) -> str:
    """Serialize `ls_structure` as indented JSON (the `--json` output)."""
    return json.dumps(ls_structure(ctx, branches, details, current), indent=2)


def arm_branch_set(
    ctx: StackerCtx,
    repo_name: str,
    target_branch: str,
    scope: Scope = "current",
) -> list[TrackedBranch]:
    """Public wrapper over `_ls_branch_set` for cross-module reuse.

    `project.status._gather_stacker` needs the exact branch set the text
    arm renders so its structured tree matches; expose the selector
    rather than re-deriving lineage there.
    """
    return _ls_branch_set(ctx, repo_name, target_branch, scope)
