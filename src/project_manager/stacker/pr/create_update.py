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
    record_pr,
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
from .template import inject_body_into_template, load_pr_template

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
    title, first_body, worktree_path = first_commit_text(ctx, tracked)
    base = pr_base_for_current_branch(ctx, tracked, config, current_repo)
    existing = find_open_pr(ctx, tracked, config, current_repo)
    head = head_ref_for_branch(config, current_repo, remote_branch)
    label = selectors.selector_for(tracked.repo_name, tracked.branch)
    if existing:
        # Don't ship a body — refresh_component_pr_bodies() runs next and
        # rewrites only the managed stacker block, preserving any edits
        # the user has made to the surrounding PR description.
        #
        # Title gets the same treatment: only sync when the live title
        # still matches the bottom-commit subject pm computes. A
        # divergence means either a manual GitHub-UI edit *or* an
        # amended bottom commit whose subject hasn't been propagated
        # yet — we can't tell which from cache, so we never clobber.
        # To pick up a new subject after amending, re-set the title
        # once with `gh pr edit --title <subject>`; subsequent pushes
        # will keep it in sync.
        fmt.record(ctx, logs, f"Updating PR #{existing.number} for {label}")
        synced_title = title if existing.title == title else None
        ctx.pr_backend.edit_pr(
            gh.EditPRRequest(
                repo=target_repo,
                number=existing.number,
                title=synced_title,
                base=base,
            )
        )
        pr_url = existing.url
    else:
        fmt.record(ctx, logs, f"Creating PR for {label}")
        # Template-injection happens at create time only; on re-push the
        # update branch above keeps `EditPRRequest.body` unset, so the
        # template-filled body the user has since edited in the GitHub
        # UI survives. Mirrors universe gitstack `inject_body_into_template`
        # at universe/ci/gitstack/src/commands/push.rs:935-958.
        template = load_pr_template(worktree_path)
        body_seed = (
            inject_body_into_template(first_body, template)
            if template is not None
            else first_body
        )
        body = compose_body_with_block(body_seed, "")
        with body_file(body) as file_path:
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
    refreshed = ctx.pr_backend.view_pr(pr_url)
    if not refreshed:
        raise git.GitError(f"Could not fetch PR {pr_url} after create/update.")
    # Cache the full PR (url + state + draft + merged) so later runs can
    # read the merge status without another round-trip.
    record_pr(ctx, tracked, refreshed)
    return refreshed


def refresh_component_pr_bodies(
    ctx: StackerCtx,
    tracked: TrackedBranch,
    pr_ctx: PrContext,
    logs: list[str],
) -> None:
    config = pr_ctx.config
    component = lineage(ctx, tracked)
    # Each iteration of the push loop calls record_pr() before returning,
    # so pr_map_for_component's cache-first lookup already sees every PR
    # we just created/updated. No focus-branch override needed — and any
    # such override would be wrong when the focus isn't the last-processed
    # branch (e.g. push from the middle of a stack with descendants).
    pr_map = pr_map_for_component(ctx, component, config, pr_ctx.current_repo)
    if not pr_map:
        return
    render_ctx = _StackRender(
        component=component,
        pr_map=pr_map,
        config=config,
        current_repo=pr_ctx.current_repo,
        live_heads=_live_heads(ctx, component),
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


def _live_heads(
    ctx: StackerCtx, component: list[TrackedBranch]
) -> dict[str, str]:
    """Map `branch -> git rev-parse <branch>` from the canonical repo.

    Source of truth for the head SHA in `_files_url`. Reads the branch
    ref directly so it tracks the latest commit regardless of which
    worktree (if any) has the branch checked out — refs are shared
    across all worktrees of a repo. The DB's `last_clean_head` is only
    refreshed by sync/init/repair, so it goes stale as soon as the user
    adds a local commit; reading the ref live closes that gap.
    Branches missing locally fall through to the `last_clean_head`
    fallback in `_files_url`.
    """
    heads: dict[str, str] = {}
    for node in component:
        repo_path = ctx.paths.repo(node.repo_name)
        try:
            heads[node.branch] = git.rev_parse(repo_path, node.branch)
        except git.GitError:
            continue
    return heads


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
