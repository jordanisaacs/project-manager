from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from project_manager.stacker import gh, git, locate, selectors
from project_manager.stacker.models import RepoPRConfig, TrackedBranch
from project_manager.stacker.ops.worktree import require_checked_out

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def push_remote_slug(ctx: StackerCtx, repo_name: str) -> str:
    return ctx.pr_backend.repo_info(cwd=ctx.paths.repo(repo_name)).name_with_owner


def head_repo_for_branch(ctx: StackerCtx, tracked: TrackedBranch) -> str | None:
    """Return the `owner/name` slug of the repo the branch is pushed to.

    Detects cross-repo same-owner fork setups (e.g.
    acme/widgets-dev → acme/widgets), which `gh pr create` cannot
    handle (cli/cli#10093). Returns None when the
    upstream isn't set, the remote URL can't be parsed, or we're in a
    test fixture without a real git remote — callers fall back to the
    default `gh pr create` path.
    """
    path = locate.locate_worktree(ctx.paths, tracked.repo_name, tracked.branch)
    if path is None:
        return None
    remote = git.upstream_remote_name(path)
    if not remote:
        return None
    url = git.remote_url(path, remote)
    if not url:
        return None
    return git.parse_github_slug(url)


def remote_branch_name(ctx: StackerCtx, tracked: TrackedBranch) -> str:
    path = require_checked_out(ctx, tracked.repo_name, tracked.branch)
    upstream = git.upstream_branch_name(path)
    if not upstream:
        raise git.GitError(
            f"{selectors.selector_for(tracked.repo_name, tracked.branch)} has no upstream "
            "remote branch. Run stacker pp or push it first."
        )
    return upstream


def target_repo_slug(config: RepoPRConfig) -> str:
    return config.target_repo


def head_ref_for_branch(
    config: RepoPRConfig,
    current_repo: gh.RepoInfo,
    remote_branch: str,
) -> str:
    if config.target_repo != current_repo.name_with_owner:
        return f"{current_repo.owner}:{remote_branch}"
    return remote_branch


def first_commit_text(
    ctx: StackerCtx, tracked: TrackedBranch
) -> tuple[str, str, Path]:
    path = require_checked_out(ctx, tracked.repo_name, tracked.branch)
    title, body = git.first_commit_title_and_body(
        path, f"{tracked.managed_base_commit}..HEAD"
    )
    if not title:
        title = tracked.branch
    return title, body, path


def pr_base_for_current_branch(
    ctx: StackerCtx,
    tracked: TrackedBranch,
    config: RepoPRConfig,
    current_repo: gh.RepoInfo,
) -> str:
    # Deferred: find.py imports target_repo_slug from here, so a top-level
    # `from .find import find_open_pr` would cycle on module load.
    from project_manager.stacker.pr.find import find_open_pr  # noqa: PLC0415

    if config.mode == "repo-pr":
        return config.trunk_branch
    if tracked.parent_branch == config.trunk_branch:
        return config.trunk_branch
    parent_tracked = ctx.db.get_branch(tracked.parent_repo_name, tracked.parent_branch)
    parent_label = selectors.selector_for(tracked.parent_repo_name, tracked.parent_branch)
    if not parent_tracked:
        raise git.GitError(f"Direct parent {parent_label} must already have an open PR.")
    parent_pr = find_open_pr(ctx, parent_tracked, config, current_repo)
    if not parent_pr:
        raise git.GitError(f"Direct parent {parent_label} must already have an open PR.")
    # Use the gh-supplied head ref instead of `remote_branch_name`, which
    # reads worktree-local `branch.<name>.merge` config and so requires the
    # parent to be checked out in a pm slot. During `pm stacker push` the
    # parent's slot is already released by the time we resolve a child's
    # base, and pools with fewer slots than tracked branches always trip it.
    if not parent_pr.head_ref_name:
        raise git.GitError(
            f"Direct parent {parent_label} has an open PR but no head ref."
        )
    return parent_pr.head_ref_name
