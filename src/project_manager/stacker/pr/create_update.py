from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from project_manager.stacker import gh, git, selectors
from project_manager.stacker.models import RepoPRConfig, TrackedBranch
from project_manager.stacker.render import format as fmt

from .find import (
    compose_body_with_block,
    find_open_pr,
    pr_base_body,
    pr_map_for_component,
    record_pr_url,
)
from .lineage import lineage
from .resolve import (
    first_commit_text,
    head_ref_for_branch,
    head_repo_for_branch,
    pr_base_for_current_branch,
    remote_branch_name,
    target_repo_slug,
)
from .stack_block import _StackRender, body_file, render_stack_block

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


@dataclass(frozen=True)
class PrContext:
    """Resolved PR-side state for a push walk: per-repo config + active gh repo.

    `config` holds the per-repo PR mode/trunk/target from stacker config;
    `current_repo` is the GitHub repo info resolved from cwd (used by
    head-ref construction for cross-fork PRs).
    """

    config: RepoPRConfig
    current_repo: gh.RepoInfo


def create_or_update_current_pr(
    ctx: StackerCtx,
    tracked: TrackedBranch,
    pr_ctx: PrContext,
    *,
    draft: bool,
    logs: list[str],
) -> gh.PullRequest:
    config = pr_ctx.config
    current_repo = pr_ctx.current_repo
    target_repo = target_repo_slug(config)
    remote_branch = remote_branch_name(ctx, tracked)
    head_repo = head_repo_for_branch(ctx, tracked)
    title, first_body = first_commit_text(ctx, tracked)
    body = compose_body_with_block(first_body, "")
    base = pr_base_for_current_branch(ctx, tracked, config, current_repo)
    existing = find_open_pr(ctx, tracked, config, current_repo)
    head = head_ref_for_branch(config, current_repo, remote_branch)
    label = selectors.selector_for(tracked.repo_name, tracked.branch)
    with body_file(body) as file_path:
        if existing:
            fmt.record(ctx, logs, f"Updating PR #{existing.number} for {label}")
            ctx.pr_backend.edit_pr(
                gh.EditPRRequest(
                    repo=target_repo,
                    number=existing.number,
                    title=title,
                    base=base,
                    body_file=file_path,
                )
            )
            pr_url = existing.url
        else:
            fmt.record(ctx, logs, f"Creating PR for {label}")
            pr_url = ctx.pr_backend.create_pr(
                gh.CreatePRRequest(
                    repo=target_repo,
                    base=base,
                    head=head,
                    title=title,
                    body_file=file_path,
                    draft=draft,
                    head_repo=head_repo,
                )
            )
    # Cache the URL on the branch so future runs skip search entirely.
    record_pr_url(ctx, tracked, pr_url)
    refreshed = ctx.pr_backend.view_pr(pr_url)
    if not refreshed:
        raise git.GitError(f"Could not fetch PR {pr_url} after create/update.")
    return refreshed


def refresh_component_pr_bodies(
    ctx: StackerCtx,
    tracked: TrackedBranch,
    pr_ctx: PrContext,
    current_pr: gh.PullRequest | None,
    logs: list[str],
) -> None:
    config = pr_ctx.config
    component = lineage(ctx, tracked)
    pr_map = pr_map_for_component(ctx, component, config, pr_ctx.current_repo)
    if current_pr:
        pr_map[tracked.branch] = current_pr
    if not pr_map:
        return
    render_ctx = _StackRender(
        component=component,
        pr_map=pr_map,
        config=config,
        current_repo=pr_ctx.current_repo,
    )
    for node in component:
        pr = pr_map.get(node.branch)
        if not pr:
            continue
        block = render_stack_block(ctx, render_ctx, node)
        base_body = pr_base_body(pr)
        with body_file(compose_body_with_block(base_body, block)) as file_path:
            ctx.pr_backend.edit_pr(
                gh.EditPRRequest(
                    repo=target_repo_slug(config),
                    number=pr.number,
                    body_file=file_path,
                )
            )
        fmt.record(
            ctx,
            logs,
            f"Updated stack block for PR #{pr.number} "
            f"({selectors.selector_for(node.repo_name, node.branch)})",
        )


def erase_component_pr_bodies(
    ctx: StackerCtx,
    tracked: TrackedBranch,
    pr_ctx: PrContext,
    logs: list[str],
) -> None:
    config = pr_ctx.config
    component = lineage(ctx, tracked)
    pr_map = pr_map_for_component(ctx, component, config, pr_ctx.current_repo)
    if not pr_map:
        return
    for node in component:
        pr = pr_map.get(node.branch)
        if not pr:
            continue
        with body_file(pr_base_body(pr)) as file_path:
            ctx.pr_backend.edit_pr(
                gh.EditPRRequest(
                    repo=target_repo_slug(config),
                    number=pr.number,
                    body_file=file_path,
                )
            )
        fmt.record(
            ctx,
            logs,
            f"Erased stack block for PR #{pr.number} "
            f"({selectors.selector_for(node.repo_name, node.branch)})",
        )
