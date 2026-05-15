from __future__ import annotations

from typing import TYPE_CHECKING

from project_manager.stacker import git, selectors
from project_manager.stacker.cherry_pick import driver as cp_driver
from project_manager.stacker.models import OperationState, SelectorTarget
from project_manager.stacker.render import format as fmt
from project_manager.stacker.render.graph import ensure_syncable

from . import worktree
from .track import require_tracked

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def absorb(ctx: StackerCtx, target: SelectorTarget) -> str:
    """Cherry-pick the current branch's new commits onto its parent.

    One level only. The parent's tip advances by `rev-list parent..child`
    (minus patch-id duplicates); the child branch is untouched. Conflicts
    pause through the shared cherry-pick driver, resumable via
    `pm stacker continue` / `abort`. Running sync on the originating child
    afterward drops the now-duplicated commits cleanly.
    """
    if ctx.db.get_operation(target.repo_name):
        raise git.GitError(
            "Another stacker operation is active for this repo. "
            "Use `pm stacker continue` or `pm stacker abort`."
        )
    tracked = require_tracked(ctx, target)
    if tracked.parent_repo_name != tracked.repo_name:
        # Cross-repo parents are a stacker capability (parent_repo_name on
        # TrackedBranch) but the op-state, worktree pool, and continue/abort
        # plumbing all key off one repo_name. Punt until there's a real need.
        raise git.GitError(
            "Absorb does not yet support cross-repo parents "
            f"({selectors.selector_for(tracked.parent_repo_name, tracked.parent_branch)} "
            f"is in a different repo than "
            f"{selectors.selector_for(tracked.repo_name, tracked.branch)})."
        )
    repo_name = tracked.repo_name
    parent_branch = tracked.parent_branch
    child_branch = tracked.branch
    repo_root = ctx.paths.repo(repo_name)
    parent_head = git.rev_parse(repo_root, parent_branch)
    child_head = git.rev_parse(repo_root, child_branch)
    commit_list = git.rev_list_picking(repo_root, parent_head, child_head)
    if not commit_list:
        child_label = selectors.selector_for(repo_name, child_branch)
        parent_label = selectors.selector_for(repo_name, parent_branch)
        return f"Nothing to absorb from {child_label} into {parent_label}."
    with worktree.acquired_for_op(ctx, repo_name, parent_branch) as acquired:
        ensure_syncable(acquired.path)
        parent_slot_head = git.rev_parse(acquired.path, "HEAD")
        ctx.db.put_operation(
            OperationState(
                repo_name=repo_name,
                op_type="local_absorb",
                status="running",
                # op.branch = the branch that's checked out in the slot (parent);
                # op.parent_branch carries the source child so failure messages
                # and `pm stacker abort` can render both sides of the absorb.
                branch=parent_branch,
                parent_branch=child_branch,
                start_head=parent_slot_head,
                target_parent_head=parent_slot_head,
                commit_list=commit_list,
                next_commit_index=0,
            )
        )
        logs: list[str] = []
        fmt.record(
            ctx,
            logs,
            f"Absorbing {len(commit_list)} commit(s) from "
            f"{selectors.selector_for(repo_name, child_branch)} into "
            f"{selectors.selector_for(repo_name, parent_branch)} "
            f"({fmt.short(parent_slot_head)})",
        )
        return cp_driver.run_until_pause_or_finish(
            ctx,
            repo_name,
            acquired,
            logs=logs,
        )
