from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from project_manager.pool.db import OwnerKind, PoolDB
from project_manager.stacker import gh, git
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

from . import format as fmt
from . import graph

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
        _refresh_online_pr_state(ctx, branches)
    if options.render.hide_merged:
        merged = {
            (pr.repo_name, pr.branch)
            for pr in ctx.db.list_pr_states(repo_name)
            if pr.merged
        }
        branches = [b for b in branches if (b.repo_name, b.branch) not in merged]
    if options.json_output:
        # `--legend` is intentionally ignored here — JSON consumers don't
        # need glyph docs, and injecting them would break the parse.
        return _ls_json(ctx, branches, options.details, options.current)
    body = (
        _empty_text(ctx, repo_name, options.current)
        if not branches
        else _ls_tree(
            ctx, branches, options.details, options.current, options.render,
        )
    )
    if options.legend:
        return body + "\n\n" + graph.render_legend(options.render)
    return body


# Pool worktrees are laid out as `<paths.worktrees>/<repo>/<uuid>/...`.
_WORKTREE_MIN_PARTS = 2


def _resolve_branch_projects(
    paths: Paths, live: dict[str, git.WorktreeInfo],
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
    pooldb: PoolDB, pool_root: Path, wt_path: Path,
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
    ctx: StackerCtx, repo_name: str | None, current: tuple[str, str] | None,
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
    return (
        f"No tracked branches in {cur_repo}. "
        f"Current branch `{cur_branch}` is not tracked by pm."
    )


def _refresh_online_pr_state(
    ctx: StackerCtx, branches: list[TrackedBranch],
) -> None:
    """Bulk-refresh `pr_state` review fields via one GraphQL call per repo.

    Groups by (owner, repo) parsed from the cached pr_url — branches
    without a cached URL are skipped (you can't fetch review state for a
    PR that doesn't exist yet). Errors fall through silently: a GraphQL
    blip shouldn't break `ls`; the renderer falls back to whatever's
    already in the cache.
    """
    cached = {
        (pr.repo_name, pr.branch): pr
        for pr in ctx.db.list_pr_states()
    }
    entries: list[tuple[tuple[str, str, int], tuple[str, str]]] = []
    for b in branches:
        pr = cached.get((b.repo_name, b.branch))
        if pr is None:
            continue
        parsed = gh.parse_pr_url(pr.pr_url)
        if parsed is None:
            continue
        entries.append((parsed, (b.repo_name, b.branch)))
    if not entries:
        return
    try:
        reviews = ctx.pr_backend.batch_pr_review([e[0] for e in entries])
    except Exception:  # noqa: BLE001  # best-effort refresh; keep rendering on any failure
        return
    for parsed, (repo, branch) in entries:
        summary = reviews.get(parsed)
        if summary is None:
            continue
        base = cached[(repo, branch)]
        ctx.db.upsert_pr_state(
            PRState(
                repo_name=repo,
                branch=branch,
                pr_url=base.pr_url,
                pr_number=base.pr_number or parsed[2],
                state=summary.state or base.state,
                is_draft=summary.is_draft,
                merged=summary.state == "MERGED" or base.merged,
                merged_at=base.merged_at,
                is_approved=summary.is_approved,
                has_open_comments=summary.has_open_comments,
            ),
        )


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
    ctx: StackerCtx,
    branches: list[TrackedBranch],
    details: Details,
    current: tuple[str, str] | None,
    render_opts: RenderOptions,
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
        # Two-space gutter mirrors the per-row `> ` current marker.
        lines.append("  " + fmt.style(repo, fg="blue", bold=True))
        tracked_branches = {item.branch for item in items}
        if (
            current is not None
            and current[0] == repo
            and current[1] not in tracked_branches
        ):
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
            if (
                item.parent_branch not in tracked_branches
                and item.parent_branch not in seen_roots
            ):
                roots.append((item.parent_branch, True))
                seen_roots.add(item.parent_branch)
        current_branch = current[1] if current and current[0] == repo else None
        pr_states = {pr.branch: pr for pr in ctx.db.list_pr_states(repo)}
        branch_projects = _resolve_branch_projects(ctx.paths, live)
        gctx = graph.GraphCtx(
            repo_name=repo,
            items=items,
            live=live,
            details=details,
            render_opts=render_opts,
            pr_states=pr_states,
            current_branch=current_branch,
            branch_projects=branch_projects,
        )
        for parent_branch, implicit in sorted(roots):
            graph.render_graph_node(
                ctx,
                lines,
                gctx,
                graph.GraphPos(
                    branch=parent_branch, prefix="", is_last=True, implicit=implicit,
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


def _ls_json(
    ctx: StackerCtx,
    branches: list[TrackedBranch],
    details: Details,
    current: tuple[str, str] | None,
) -> str:
    """Hierarchical JSON: `{current_branch, branches: [root...with children]}`.

    Tiered fields mirror `--details`: `none` only structural fields,
    `status` adds status/needs_sync/ahead_of_remote, `status-counts`
    adds commit_count, `all` surfaces cached git SHAs for debugging.
    """
    include_state = details in ("status", "status-counts", "all")
    include_counts = details in ("status-counts", "all")
    include_all = details == "all"
    pr_map: dict[tuple[str, str], PRState] = {
        (pr.repo_name, pr.branch): pr
        for pr in ctx.db.list_pr_states()
    }
    tracked_keys: set[tuple[str, str]] = {(b.repo_name, b.branch) for b in branches}
    children_map: dict[tuple[str, str], list[TrackedBranch]] = {}
    for b in branches:
        children_map.setdefault((b.parent_repo_name, b.parent_branch), []).append(b)
    # Per-repo GraphCtx keeps the NodeStatus builder shareable with the
    # text renderer. Online is forced false for JSON: the JSON path
    # reflects cached state only, matching gitstack's emit-don't-fetch
    # semantics for machine-readable output.
    json_render = RenderOptions(online=False)
    gctx_by_repo: dict[str, graph.GraphCtx] = {}
    for repo in {b.repo_name for b in branches}:
        try:
            live = {
                info.branch: info
                for info in git.worktree_list(ctx.paths.repo(repo))
                if info.branch
            }
        except git.GitError:
            live = {}
        gctx_by_repo[repo] = graph.GraphCtx(
            repo_name=repo,
            items=[b for b in branches if b.repo_name == repo],
            live=live,
            details=details,
            render_opts=json_render,
            pr_states={b: pr_map[(repo, b)] for (r, b) in pr_map if r == repo},
            current_branch=current[1] if current and current[0] == repo else None,
        )

    def node(b: TrackedBranch) -> dict[str, object]:
        pr = pr_map.get((b.repo_name, b.branch))
        gctx = gctx_by_repo[b.repo_name]
        status = graph.build_node_status(ctx, gctx, b, pr=pr, details=details)
        entry: dict[str, object] = {
            "repo_name": b.repo_name,
            "branch": b.branch,
            "parent_repo_name": b.parent_repo_name,
            "parent_branch": b.parent_branch,
            "is_root": (b.parent_repo_name, b.parent_branch) not in tracked_keys,
            "pr_url": pr.pr_url if pr is not None else None,
            "merged": pr.merged if pr is not None else False,
            "status": _STATUS_JSON[
                graph.classify(b, pr, status, online=False)
            ] if include_state else None,
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
    return json.dumps(
        {
            "current_branch": current[1] if current is not None else None,
            "branches": [node(r) for r in roots],
        },
        indent=2,
    )


