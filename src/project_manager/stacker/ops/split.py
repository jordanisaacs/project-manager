from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from project_manager.pool import slot as slot_mod
from project_manager.stacker import git
from project_manager.stacker.models import SelectorTarget, TrackedBranch
from project_manager.stacker.render import format as fmt

from . import worktree
from .track import require_tracked

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def split(
    ctx: StackerCtx,
    target: SelectorTarget,
    new_name: str,
    split_commit: str,
    *,
    stay: bool = False,
) -> str:
    """Split `[split_commit..HEAD]` onto a new child branch `new_name`.

    Front-to-back: earlier commits stay on `target`, `split_commit`
    and everything after it move to `new_name`. Children of `target`
    reparent onto `new_name` (they were based on target's old tip,
    which is now `new_name`'s tip). Refuses dirty worktrees and any
    attempt to split at the first commit of the branch.

    After the split, `new_name` is provisioned into a free pool slot
    so the user has a workspace for it. Pass `stay=True` to skip
    the slot claim (the branch ref and DB row still land) — useful
    when the pool is saturated or the user only wanted to rearrange
    history. A saturated pool under the default is surfaced in the
    result message rather than raising, since the split itself has
    already succeeded.
    """
    tracked, current_path, plan = _plan_split(ctx, target, new_name, split_commit)
    split_sha, head_sha, new_base_sha, moved_count = plan
    git.git(current_path, "branch", new_name, head_sha)
    git.reset_hard(current_path, new_base_sha)
    children = ctx.db.get_children(tracked.repo_name, tracked.branch)
    ctx.db.upsert_branch(
        TrackedBranch(
            repo_name=tracked.repo_name,
            branch=new_name,
            parent_repo_name=tracked.repo_name,
            parent_branch=tracked.branch,
            managed_base_commit=new_base_sha,
            last_synced_parent_commit=new_base_sha,
            last_clean_head=head_sha,
        )
    )
    for child in children:
        if child.branch == new_name:
            continue
        ctx.db.upsert_branch(
            TrackedBranch(
                repo_name=child.repo_name,
                branch=child.branch,
                parent_repo_name=tracked.repo_name,
                parent_branch=new_name,
                managed_base_commit=child.managed_base_commit,
                last_synced_parent_commit=child.last_synced_parent_commit,
                last_clean_head=child.last_clean_head,
            )
        )
    header = (
        f"Split {tracked.branch} at {fmt.short(split_sha)}: "
        f"{moved_count} commit(s) moved to {new_name}."
    )
    if stay:
        return header
    try:
        worktree.acquire(ctx, tracked.repo_name, new_name)
    except slot_mod.PoolExhaustedError as exc:
        return f"{header}\nNote: could not claim a slot for {new_name}: {exc}"
    return f"{header}\n{new_name} is now checked out in a fresh slot."


def _plan_split(
    ctx: StackerCtx,
    target: SelectorTarget,
    new_name: str,
    split_commit: str,
) -> tuple[TrackedBranch, Path, tuple[str, str, str, int]]:
    """Validate preconditions and compute the four sha/count values split needs.

    Returns (tracked, worktree_path, (split_sha, head_sha, new_base_sha,
    moved_count)). Raises on any precondition violation before any ref
    or DB state is mutated.
    """
    if ctx.db.get_operation(target.repo_name):
        raise git.GitError(
            "Another stacker operation is active for this repo. "
            "Use 'stacker continue' or 'stacker abort'."
        )
    tracked = require_tracked(ctx, target)
    current_path = worktree.require_checked_out(
        ctx, tracked.repo_name, tracked.branch
    )
    if git.has_tracked_changes(current_path):
        raise git.GitError(
            f"Tracked changes present in {current_path}. Commit or discard before splitting."
        )
    if git.cherry_pick_in_progress(current_path):
        raise git.GitError(
            f"Cherry-pick in progress in {current_path}. Resolve before splitting."
        )
    if git.branch_exists(ctx.paths.repo(tracked.repo_name), new_name):
        raise git.GitError(f"Branch {new_name} already exists.")
    split_sha = git.rev_parse(current_path, split_commit)
    head_sha = git.rev_parse(current_path, "HEAD")
    commit_list = git.rev_list(
        current_path, f"{tracked.managed_base_commit}..{head_sha}",
    )
    if split_sha not in commit_list:
        raise git.GitError(
            f"commit {split_commit} is not on {tracked.branch} "
            f"between {fmt.short(tracked.managed_base_commit)} and HEAD."
        )
    split_index = commit_list.index(split_sha)
    if split_index == 0:
        raise git.GitError("Cannot split at the first commit of this branch.")
    new_base_sha = commit_list[split_index - 1]
    moved_count = len(commit_list) - split_index
    return tracked, current_path, (split_sha, head_sha, new_base_sha, moved_count)
