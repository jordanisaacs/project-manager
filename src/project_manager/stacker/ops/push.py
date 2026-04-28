from __future__ import annotations

from typing import TYPE_CHECKING

from project_manager.stacker import gh, git
from project_manager.stacker.models import DEFAULT_PUSH_OPTIONS, PushOptions, SelectorTarget
from project_manager.stacker.pr import create_update as pr_create_update
from project_manager.stacker.pr.config import pr_config
from project_manager.stacker.pr.create_update import PrContext
from project_manager.stacker.pr.lineage import resolve_scope
from project_manager.stacker.render import format as fmt

from . import worktree
from .track import require_tracked

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def push(
    ctx: StackerCtx,
    target: SelectorTarget,
    options: PushOptions = DEFAULT_PUSH_OPTIONS,
) -> str:
    """Force-push and create/update PRs for a set of tracked branches.

    Scope resolution matches `sync`; within the resolved list each
    branch is force-pushed and has its PR created or updated.
    `options.publish` overrides per-branch draft decisions: default
    draft behavior is only-leaf-non-draft; with `--publish` every PR
    is published, with `--draft` every PR is draft.
    `options.create_pr=False` skips PR creation and only force-pushes.
    """
    if options.publish and options.draft:
        raise git.GitError("--publish and --draft are mutually exclusive.")
    if ctx.db.get_operation(target.repo_name):
        raise git.GitError(
            "Another stacker operation is active for this repo. "
            "Use `pm stacker continue` or `pm stacker abort`."
        )
    resolved = resolve_scope(ctx, target.repo_name, target.branch, options.scope)
    if not resolved:
        return "No tracked branches to push."
    config = pr_config(ctx, target.repo_name)
    repo_path = ctx.paths.repo(target.repo_name)
    logs: list[str] = []
    pr_ctx: PrContext | None = (
        PrContext(config=config, current_repo=ctx.pr_backend.repo_info(cwd=repo_path))
        if options.create_pr
        else None
    )
    leaf = resolved[-1]
    focus_pr: gh.PullRequest | None = None
    for item in resolved:
        # `acquired_for_op` releases the slot on exit when the worktree
        # has nothing to preserve — covers success, exception, and Ctrl+C
        # uniformly. Push has no resumable state of its own; if a cherry
        # -pick somehow remains in progress at exit time, the predicate
        # holds the slot for `pm stacker continue` to pick up.
        with worktree.acquired_for_op(ctx, item.repo_name, item.branch):
            worktree.run_single_pp(ctx, item, logs)
            if pr_ctx is not None:
                is_leaf = item.branch == leaf.branch
                item_draft = _pr_draft_decision(
                    draft=options.draft,
                    publish=options.publish,
                    is_leaf=is_leaf,
                )
                pr = pr_create_update.create_or_update_current_pr(
                    ctx, item, pr_ctx, draft=item_draft, logs=logs
                )
                if item.branch == target.branch:
                    focus_pr = pr
    if pr_ctx is None:
        return fmt.finish(ctx, logs, "Push complete.")
    assert focus_pr is not None
    refreshed = require_tracked(ctx, target)
    pr_create_update.refresh_component_pr_bodies(ctx, refreshed, pr_ctx, logs)
    return fmt.finish(ctx, logs, f"PR ready: {focus_pr.url}")


def _pr_draft_decision(*, draft: bool, publish: bool, is_leaf: bool) -> bool:
    """Pick draft-ness per-branch when push walks a scope.

    `--draft` → every PR draft. `--publish` → every PR published.
    Default → non-leaf entries draft, leaf published.
    """
    if draft:
        return True
    if publish:
        return False
    return not is_leaf
