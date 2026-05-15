from __future__ import annotations

from typing import TYPE_CHECKING

from project_manager.stacker import git, selectors, slot
from project_manager.stacker.models import ParentLocator, SelectorTarget, TrackedBranch
from project_manager.stacker.pr import find as pr_find
from project_manager.stacker.pr.config import pr_config
from project_manager.stacker.slot import AcquiredSlot, CwdReusePolicy

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def require_tracked(ctx: StackerCtx, target: SelectorTarget) -> TrackedBranch:
    tracked = ctx.db.get_branch(target.repo_name, target.branch)
    if not tracked:
        raise git.GitError(
            f"{selectors.selector_for(target.repo_name, target.branch)} is not tracked. "
            "Use `pm stacker create` to start a new stacked branch."
        )
    return tracked


def create_tracked_branch(
    ctx: StackerCtx,
    repo_name: str,
    branch: str,
    parent: ParentLocator,
    *,
    copy_from: str | None = None,
) -> TrackedBranch:
    """Create a branch ref in the repo and track it without claiming a slot.

    Backs `create --no-checkout`: the branch exists in git's ref
    store and the stacker DB, but no pool slot is allocated for it.
    """
    if parent.repo_name != repo_name:
        raise git.GitError("Parent and child must be in the same repo.")
    repo_path = ctx.paths.repo(repo_name)
    if git.branch_exists(repo_path, branch):
        raise git.GitError(f"Branch {branch} already exists.")
    parent_head = git.rev_parse(repo_path, parent.branch)
    start_point = git.rev_parse(repo_path, copy_from) if copy_from else parent_head
    git.git(repo_path, "branch", branch, start_point)
    tracked = TrackedBranch(
        repo_name=repo_name,
        branch=branch,
        parent_repo_name=parent.repo_name,
        parent_branch=parent.branch,
        managed_base_commit=parent_head,
        last_synced_parent_commit=parent_head,
        last_clean_head=start_point,
    )
    ctx.db.upsert_branch(tracked)
    return tracked


def track(ctx: StackerCtx, target: SelectorTarget, parent: ParentLocator) -> TrackedBranch:
    """Adopt an existing branch into stacker tracking.

    Slot resolution mirrors fresh `create` (`allow_branch_switch=True`) so
    `pm stacker create --replace <b>` can adopt `<b>` from the cwd slot
    even when `<b>` isn't checked out anywhere yet — the user typed
    `--replace`, that's the opt-in. On success the slot stays claimed
    (the adopted branch lives there); on failure it's released to undo
    a wasted `ops_slot` acquisition.
    """
    if target.repo_name != parent.repo_name:
        raise git.GitError("Parent and child must be in the same repo.")
    acquired = slot.resolve_slot(
        ctx,
        target.repo_name,
        target.branch,
        cwd_reuse=CwdReusePolicy(allow_branch_switch=True),
    )
    try:
        tracked = _persist_adoption(ctx, target, parent, acquired)
    except Exception:
        slot.release_if_owned(ctx, acquired)
        raise
    _try_link_existing_pr(ctx, tracked)
    return tracked


def _try_link_existing_pr(ctx: StackerCtx, tracked: TrackedBranch) -> None:
    """Best-effort: if the adopted branch already has an open PR on GitHub,
    cache it so `pm stacker ls` shows the URL right away.

    Adoption flows often pick up a branch that someone else (or an
    earlier `git pp`) already pushed and opened a PR for — without this,
    the row stays `[LOCAL]` until the next push. Failure modes (no
    upstream, no remote, gh CLI absent, network blip) all silently
    no-op: discovery is a convenience, not a precondition for adoption.
    """
    try:
        repo_config = pr_config(ctx, tracked.repo_name)
        repo_path = ctx.paths.repo(tracked.repo_name)
        current_repo = ctx.pr_backend.repo_info(cwd=repo_path)
    except Exception:  # noqa: BLE001
        return
    try:
        pr_find.find_pr(ctx, tracked, repo_config, current_repo)
    except Exception:  # noqa: BLE001
        return


def _persist_adoption(
    ctx: StackerCtx,
    target: SelectorTarget,
    parent: ParentLocator,
    acquired: AcquiredSlot,
) -> TrackedBranch:
    repo_path = ctx.paths.repo(target.repo_name)
    base_commit = git.merge_base(repo_path, parent.branch, target.branch)
    parent_head = git.rev_parse(repo_path, parent.branch)
    tracked = TrackedBranch(
        repo_name=target.repo_name,
        branch=target.branch,
        parent_repo_name=parent.repo_name,
        parent_branch=parent.branch,
        managed_base_commit=base_commit,
        last_synced_parent_commit=parent_head,
        last_clean_head=git.rev_parse(acquired.path, "HEAD"),
    )
    ctx.db.upsert_branch(tracked)
    return tracked
