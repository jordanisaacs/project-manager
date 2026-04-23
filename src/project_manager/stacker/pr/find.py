from __future__ import annotations

import re
from typing import TYPE_CHECKING

from project_manager.stacker import gh, git, locate
from project_manager.stacker.models import RepoPRConfig, TrackedBranch

from .resolve import target_repo_slug

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def pr_map_for_component(
    ctx: StackerCtx,
    component: list[TrackedBranch],
    config: RepoPRConfig,
    current_repo: gh.RepoInfo,
) -> dict[str, gh.PullRequest]:
    """Find the PR (any state) for every branch in the component.

    Used by stack-block rendering, which wants to surface merged PRs
    with a "[merged]" label rather than dropping them silently.
    """
    out: dict[str, gh.PullRequest] = {}
    for node in component:
        pr = find_pr(ctx, node, config, current_repo)
        if pr is not None:
            out[node.branch] = pr
    return out


def find_open_pr(
    ctx: StackerCtx,
    tracked: TrackedBranch,
    config: RepoPRConfig,
    current_repo: gh.RepoInfo,
) -> gh.PullRequest | None:
    """Return the OPEN PR for `tracked`, or None.

    Callers use this to decide between create-vs-update: a closed
    or merged cached PR returns None here so a fresh PR is opened.
    """
    pr = find_pr(ctx, tracked, config, current_repo)
    return pr if pr is not None and pr.state == "OPEN" else None


def find_pr(
    ctx: StackerCtx,
    tracked: TrackedBranch,
    config: RepoPRConfig,
    current_repo: gh.RepoInfo,  # noqa: ARG001 (interface parity with find_open_pr)
) -> gh.PullRequest | None:
    """Return the PR for `tracked` in any state (open/closed/merged).

    Cache-first: if `tracked.pr_url` is set we trust it and hit the
    REST single-PR endpoint — no dependency on GitHub's search index.
    Falls back to a GraphQL-style search
    (`repo:X head:Y is:pr is:open`) only when the cache is empty;
    writes the URL back on a hit so the next run is cache-only.
    """
    if tracked.pr_url:
        cached = ctx.pr_backend.view_pr(tracked.pr_url)
        if cached is not None:
            return cached
    target_repo = target_repo_slug(config)
    path = locate.locate_worktree(ctx.paths, tracked.repo_name, tracked.branch)
    if path is None:
        return None
    remote_branch = git.upstream_branch_name(path)
    if not remote_branch:
        return None
    # GraphQL search (not `gh pr list`): REST list misses cross-fork
    # same-owner PRs and was the source of the universe regression
    # where PR A was invisible to PR B's body rendering.
    query = f"repo:{target_repo} head:{remote_branch} is:pr is:open"
    prs = ctx.pr_backend.search_prs(query)
    if not prs:
        return None
    found = prs[0]
    # First-time discovery (e.g. PR opened by another tool): persist
    # the URL so subsequent runs skip the search entirely.
    if not tracked.pr_url:
        record_pr_url(ctx, tracked, found.url)
    return found


def record_pr_url(ctx: StackerCtx, tracked: TrackedBranch, url: str) -> None:
    ctx.db.upsert_branch(
        TrackedBranch(
            repo_name=tracked.repo_name,
            branch=tracked.branch,
            parent_repo_name=tracked.parent_repo_name,
            parent_branch=tracked.parent_branch,
            managed_base_commit=tracked.managed_base_commit,
            last_synced_parent_commit=tracked.last_synced_parent_commit,
            last_clean_head=tracked.last_clean_head,
            pr_url=url,
        )
    )


def strip_managed_block(body: str) -> str:
    return re.sub(
        r"\n?<!-- stacker:begin -->.*?<!-- stacker:end -->\n?",
        "\n",
        body,
        flags=re.DOTALL,
    ).strip()


def compose_body_with_block(body: str, block: str) -> str:
    cleaned = strip_managed_block(body).strip()
    if block:
        return f"{cleaned}\n\n{block}" if cleaned else block
    return cleaned


def pr_base_body(pr: gh.PullRequest) -> str:
    return strip_managed_block(pr.body)
