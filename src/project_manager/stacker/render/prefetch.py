"""Concurrent prefetch of every fact `_ls_tree` needs before it renders.

Today's render loop calls `build_node_status` per node, which fires ~9 git
subprocesses in series — `has_tracked_changes` alone costs 3 fsmonitor
queries. On a pool with a few large-monorepo worktrees that serial chain
dominates wall time for `pm stacker ls`. This module precomputes every
per-repo and per-node fact concurrently (bounded by
`[concurrency].limit`) and delivers them to the renderer as a
`{repo_name: RepoFacts}` mapping — turning render into zero-git-call
dict lookups.

All git calls go through `asyncio.to_thread(sync_fn, …)` rather than
maintaining async-variant helpers in `stacker/git.py`. That matches the
threading pattern `repo.maintenance` already uses for `run(stream=True)`
and keeps blast radius small; if the thread pool overhead becomes
visible in a profile we can port the hot sync helpers to `run_async`
without changing this module's shape.

`GitError`s from the underlying git calls are caught per-task and
degraded to the same defaults `has_graph_blocking_changes` /
`count_ahead_of_remote` / `is_synced` / `_count_branch_commits`
individually use today, so a single broken worktree can't fail the
whole prefetch or the render pass.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from project_manager.async_util import bounded_gather
from project_manager.stacker import git

from .graph import NodeStatus, has_graph_blocking_changes

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx
    from project_manager.stacker.models import (
        Details,
        PRState,
        TrackedBranch,
    )


@dataclass(frozen=True)
class RepoFacts:
    """All per-repo state `_ls_tree` needs to render one repo's tree.

    `live`, `pr_states`, `branch_projects` were previously built inline
    inside the `_ls_tree` loop; `node_status` replaces the per-node call
    to `build_node_status`.
    """

    live: dict[str, git.WorktreeInfo]
    pr_states: dict[str, PRState]
    branch_projects: dict[str, str]
    node_status: dict[str, NodeStatus] = field(default_factory=dict)


def _is_synced(ctx: StackerCtx, tracked: TrackedBranch) -> bool:
    """Sync, error-tolerant port of `graph.is_synced`.

    Lifted here rather than imported from `graph` so the prefetch doesn't
    need to route through the render module for a single rev-parse.
    Matches `graph.is_synced`'s behavior: parent-branch resolution failure
    returns False (branch is "unsynced" by default).
    """
    try:
        parent_head = git.rev_parse(
            ctx.paths.repo(tracked.parent_repo_name),
            tracked.parent_branch,
        )
    except git.GitError:
        return False
    return parent_head == tracked.managed_base_commit


def _safe_rev_count(path: Path, revspec: str) -> int:
    """`git rev-list --count`, returning 0 on any GitError.

    Matches `count_ahead_of_remote` / `_count_branch_commits` degradation
    (both return 0/None on error today). Callers treat the sentinel as
    "don't know" for rendering purposes.
    """
    with contextlib.suppress(git.GitError):
        return git.rev_count(path, revspec)
    return 0


def _safe_upstream_branch(path: Path) -> str | None:
    """`git.upstream_branch` with `GitError` folded to None."""
    with contextlib.suppress(git.GitError):
        return git.upstream_branch(path)
    return None


@dataclass(frozen=True)
class _PrefetchOpts:
    """Cross-repo knobs threaded through every layer of the prefetch.

    `details` and `current` flow unchanged from the command options down
    into per-node fact computation; `limit` is the concurrency cap
    (normally `[concurrency].limit`) shared by both the cross-repo and
    per-repo bounded fanouts.
    """

    details: Details
    current: tuple[str, str] | None
    limit: int


@dataclass(frozen=True)
class _NodeInputs:
    """Everything `_prefetch_node` needs beyond `ctx`.

    Bundled into one struct so `_prefetch_node`'s signature stays within
    the project's arity budget; all five fields are per-node immutable.
    """

    tracked: TrackedBranch
    wt_info: git.WorktreeInfo | None
    pr: PRState | None
    details: Details
    current_branch: str | None


async def _prefetch_node(ctx: StackerCtx, ni: _NodeInputs) -> NodeStatus:
    """Concurrent port of `build_node_status` for a single branch.

    Shape matches the old function exactly — same `NodeStatus` fields,
    same defaults for missing worktree / `details=="none"` — so the
    renderer's consumption of it is unchanged.
    """
    wt_path = ni.wt_info.path if ni.wt_info is not None else None
    is_current = ni.current_branch == ni.tracked.branch
    merged = ni.pr is not None and ni.pr.merged

    # `is_synced` runs for every node regardless of `details`, matching
    # build_node_status. It doesn't hit the working tree (pure ref
    # lookup on the parent repo) so it's cheap.
    if wt_path is None or ni.details == "none":
        synced = await asyncio.to_thread(_is_synced, ctx, ni.tracked)
        return NodeStatus(
            synced=synced,
            commit_count=0,
            ahead_of_remote=0,
            has_upstream=False,
            dirty=False,
            is_current=is_current,
            merged=merged,
        )

    # Fan out the independent calls: is_synced (parent-repo rev-parse),
    # dirty (one `git status` post-collapse), upstream_branch (full
    # "remote/branch" form — has_upstream is derived from it), and the
    # commit-count rev-list (only needed for status-counts/all).
    need_counts = ni.details in ("status-counts", "all")
    async with asyncio.TaskGroup() as tg:
        t_synced = tg.create_task(asyncio.to_thread(_is_synced, ctx, ni.tracked))
        t_dirty = tg.create_task(
            asyncio.to_thread(has_graph_blocking_changes, wt_path),
        )
        t_upstream = tg.create_task(
            asyncio.to_thread(_safe_upstream_branch, wt_path),
        )
        t_commits = (
            tg.create_task(
                asyncio.to_thread(
                    _safe_rev_count,
                    wt_path,
                    f"{ni.tracked.managed_base_commit}..HEAD",
                )
            )
            if need_counts
            else None
        )

    synced = t_synced.result()
    dirty = t_dirty.result()
    upstream_full = t_upstream.result()
    has_upstream = upstream_full is not None
    commit_count = t_commits.result() if t_commits is not None else 0

    # ahead_of_remote depends on upstream_full — launched serially after
    # the inner TaskGroup. One extra round-trip; acceptable because it's
    # a single rev-list call and 99% of branches have an upstream.
    if upstream_full:
        ahead_of_remote = await asyncio.to_thread(
            _safe_rev_count,
            wt_path,
            f"{upstream_full}..HEAD",
        )
    else:
        ahead_of_remote = 0

    return NodeStatus(
        synced=synced,
        commit_count=commit_count,
        ahead_of_remote=ahead_of_remote,
        has_upstream=has_upstream,
        dirty=dirty,
        is_current=is_current,
        merged=merged,
    )


async def _prefetch_repo(
    ctx: StackerCtx,
    repo_name: str,
    items: list[TrackedBranch],
    opts: _PrefetchOpts,
) -> RepoFacts:
    """Fetch `live` + DB lookups + per-node facts for one repo concurrently."""
    from .ls import _resolve_branch_projects  # noqa: PLC0415 — avoid import cycle

    repo_path = ctx.paths.repo(repo_name)

    def _live_sync() -> dict[str, git.WorktreeInfo]:
        try:
            return {info.branch: info for info in git.worktree_list(repo_path) if info.branch}
        except git.GitError:
            return {}

    live = await asyncio.to_thread(_live_sync)

    pr_states, branch_projects = await asyncio.gather(
        asyncio.to_thread(
            lambda: {pr.branch: pr for pr in ctx.db.list_pr_states(repo_name)},
        ),
        asyncio.to_thread(_resolve_branch_projects, ctx.paths, live),
    )

    current_branch = (
        opts.current[1] if opts.current is not None and opts.current[0] == repo_name else None
    )

    async def _node(item: TrackedBranch) -> tuple[str, NodeStatus]:
        status = await _prefetch_node(
            ctx,
            _NodeInputs(
                tracked=item,
                wt_info=live.get(item.branch),
                pr=pr_states.get(item.branch),
                details=opts.details,
                current_branch=current_branch,
            ),
        )
        return item.branch, status

    # Bound the per-repo node fanout — one repo on a large stack can
    # spawn tens of nodes, each firing 3-4 git children via to_thread.
    pairs = await bounded_gather(
        (_node(item) for item in items),
        limit=opts.limit,
    )
    return RepoFacts(
        live=live,
        pr_states=pr_states,
        branch_projects=branch_projects,
        node_status=dict(pairs),
    )


async def _prefetch_all_async(
    ctx: StackerCtx,
    by_repo: dict[str, list[TrackedBranch]],
    opts: _PrefetchOpts,
) -> dict[str, RepoFacts]:
    repo_names = list(by_repo.keys())

    async def _one(name: str) -> tuple[str, RepoFacts]:
        return name, await _prefetch_repo(ctx, name, by_repo[name], opts)

    # Same `limit` governs both cross-repo and per-repo fanout. The
    # semaphores don't compose (a node-level await doesn't release the
    # repo-level slot), so the effective in-flight cap is `limit^2` in
    # the worst case. That's fine in practice: `limit=10` would allow up
    # to 100 git children, and each spends most of its wall time in an
    # fsmonitor read — CPU stays low.
    pairs = await bounded_gather(
        (_one(name) for name in repo_names),
        limit=opts.limit,
    )
    return dict(pairs)


def prefetch_all(
    ctx: StackerCtx,
    by_repo: dict[str, list[TrackedBranch]],
    *,
    details: Details,
    current: tuple[str, str] | None,
    limit: int,
) -> dict[str, RepoFacts]:
    """Sync entry point: `asyncio.run` the prefetch and return the result.

    `_ls_tree` / `_ls_json` are sync — the CLI entry is sync all the way
    down, so spinning up a fresh event loop here is safe (no outer loop
    to conflict with). Keeping this wrapper sync means every existing
    `ls_text` caller stays sync.
    """
    opts = _PrefetchOpts(details=details, current=current, limit=limit)
    return asyncio.run(_prefetch_all_async(ctx, by_repo, opts))
