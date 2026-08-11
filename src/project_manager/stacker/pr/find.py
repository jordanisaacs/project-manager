from __future__ import annotations

import re
from typing import TYPE_CHECKING

from project_manager.stacker import gh, git, locate
from project_manager.stacker.models import PRState, RepoPRConfig, TrackedBranch

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

    Cache-first: if `pr_state` has a row for `tracked` we trust the URL
    and hit the REST single-PR endpoint — no dependency on GitHub's
    search index. Falls back to a GraphQL-style search
    (`repo:X head:Y is:pr is:open`) only when the cache is empty;
    writes the URL back on a hit so the next run is cache-only.
    """
    backend = ctx.pr_backend_for(tracked.repo_name)
    cached_state = ctx.db.get_pr_state(tracked.repo_name, tracked.branch)
    if cached_state is not None:
        cached = backend.view_pr(cached_state.pr_url)
        if cached is not None:
            record_pr(ctx, tracked, cached)
            return cached
    target_repo = target_repo_slug(config)
    path = locate.locate_worktree(ctx.paths, tracked.repo_name, tracked.branch)
    if path is None:
        return None
    remote_branch = git.upstream_branch_name(path)
    if not remote_branch:
        return None
    # GraphQL search (not `gh pr list`): REST list misses cross-fork
    # same-owner PRs, which would render one stacked PR invisible to
    # another's body-rendering pass.
    query = f"repo:{target_repo} head:{remote_branch} is:pr is:open"
    prs = backend.search_prs(query)
    if not prs:
        return None
    found = prs[0]
    # First-time discovery (e.g. PR opened by another tool): persist the
    # full state so subsequent runs skip the search entirely.
    if cached_state is None:
        record_pr(ctx, tracked, found)
    return found


def refresh_pr(
    ctx: StackerCtx,
    tracked: TrackedBranch,
    config: RepoPRConfig,
    current_repo: gh.RepoInfo,
) -> gh.PullRequest | None:
    """Drop the cached row and re-run discovery from scratch.

    `find_pr` short-circuits on a cached URL, so a stale or
    no-longer-current cache row would otherwise hide the real PR. This
    forces a fresh `repo:X head:Y is:pr is:open` search and persists
    the hit (or returns None when nothing is open).
    """
    ctx.db.delete_pr_state(tracked.repo_name, tracked.branch)
    return find_pr(ctx, tracked, config, current_repo)


def record_pr_url(ctx: StackerCtx, tracked: TrackedBranch, url: str) -> None:
    """Persist only a PR URL (when the full state isn't available yet).

    Preferred callers use `record_pr` with a full `PullRequest`; this
    thinner entry point exists for flows that have nothing but the URL.
    State defaults to `OPEN`; the next refresh overwrites it.
    """
    ctx.db.upsert_pr_state(
        PRState(
            repo_name=tracked.repo_name,
            branch=tracked.branch,
            pr_url=url,
            state="OPEN",
        ),
    )


def record_pr(ctx: StackerCtx, tracked: TrackedBranch, pr: gh.PullRequest) -> None:
    """Persist a full `PullRequest` into the pr_state cache."""
    is_merged = pr.state.upper() == "MERGED"
    ctx.db.upsert_pr_state(
        PRState(
            repo_name=tracked.repo_name,
            branch=tracked.branch,
            pr_url=pr.url,
            pr_number=pr.number,
            state=pr.state.upper(),
            is_draft=pr.is_draft,
            merged=is_merged,
        ),
    )


def strip_managed_block(body: str) -> str:
    """Remove the `<!-- stacker:begin/end -->`-wrapped managed block.

    HTML-comment markers are our authoritative boundary — they're
    invisible in rendered markdown and cannot collide with user content
    the way a `## 🥞` heading or `---------` rule could.
    """
    return re.sub(
        r"\n?<!-- stacker:begin -->.*?<!-- stacker:end -->\n?",
        "\n",
        body,
        flags=re.DOTALL,
    ).strip()


def compose_body_with_block(body: str, block: str) -> str:
    """Combine the managed block (already marker-wrapped) with user body.

    Block leads, user content follows: matches the gitstack-format
    layout ReviewStack's parser expects (header at the top, `---------`
    separator before the commit message body).
    """
    cleaned = strip_managed_block(body).strip()
    if not block:
        return cleaned
    return f"{block}\n\n{cleaned}" if cleaned else block


def pr_base_body(pr: gh.PullRequest) -> str:
    return strip_managed_block(pr.body)
