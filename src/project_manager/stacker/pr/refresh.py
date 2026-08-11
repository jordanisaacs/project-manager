"""Bulk `pr_state` refresh shared by `ls` (for review icons) and `sync`
(for the merged-PR collapse gate).

`refresh_review_state` was lifted out of `render/ls.py` so the sync flow
can call the same one-batch-per-repo path. Sync runs it once at the top
of the operation against every branch in scope, then per-branch gate
checks read the freshly-updated `pr_state` rows from SQLite — no network
calls in the per-branch path.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from project_manager.stacker import gh
from project_manager.stacker.models import PRState

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx
    from project_manager.stacker.models import TrackedBranch


def refresh_review_state(
    ctx: StackerCtx,
    branches: list[TrackedBranch],
) -> None:
    """Bulk-refresh `pr_state` rows via one GraphQL call per (owner, repo).

    Groups by (owner, repo) parsed from the cached pr_url — branches
    without a cached URL are skipped (you can't fetch review state for a
    PR that doesn't exist yet). Errors fall through silently so a
    GraphQL blip can't break `ls` or `sync`; callers fall back to
    whatever's already in the cache.
    """
    cached = {(pr.repo_name, pr.branch): pr for pr in ctx.db.list_pr_states()}
    entries: list[tuple[tuple[str, str, int], tuple[str, str]]] = []
    entries_by_repo: dict[str, list[tuple[str, str, int]]] = {}
    for b in branches:
        pr = cached.get((b.repo_name, b.branch))
        if pr is None:
            continue
        parsed = gh.parse_pr_url(pr.pr_url)
        if parsed is None:
            continue
        entries.append((parsed, (b.repo_name, b.branch)))
        entries_by_repo.setdefault(b.repo_name, []).append(parsed)
    if not entries:
        return
    reviews_by_repo: dict[str, dict[tuple[str, str, int], gh.PRReviewSummary]] = {}
    for repo_name, repo_entries in entries_by_repo.items():
        # Best-effort refresh: a failure for one repo should not prevent
        # differently-authenticated repos in the same walk from refreshing.
        with contextlib.suppress(Exception):
            reviews_by_repo[repo_name] = ctx.pr_backend_for(repo_name).batch_pr_review(repo_entries)
    for parsed, (repo, branch) in entries:
        summary = reviews_by_repo.get(repo, {}).get(parsed)
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
