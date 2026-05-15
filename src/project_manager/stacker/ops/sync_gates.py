"""Pre-sync safety gates and the merged-PR collapse decision.

Two preflight checks run per branch before the cherry-pick path:

- `check_parent_modifications`: errors when the branch's
  `last_synced_parent_commit` is no longer an ancestor of the slot's
  HEAD. Catches out-of-band history surgery below the stack range
  (e.g. `git rebase --onto`, amends touching pre-stack commits). The
  `--allow-drop-parent-modifications` flag bypasses the gate and lets
  sync drop the below-anchor changes.

- `merged_collapse_action`: when the branch's PR is `MERGED`, decides
  between auto-collapse-to-parent (squash present, or flag set) and
  gate-error (squash missing and `--allow-drop-merge` unset).

`run_branch_gates` is the entry point for both single-branch sync and
the downstream queue: runs the two checks and raises `GitError` on a
gate that the caller hasn't opted out of. `collapse_if_merged` then
performs the reset-to-parent when the merged-PR check returned
`"collapse"`. Both live here (not in `ops/sync.py`) so the cherry-pick
driver can call them without re-introducing the
ops/sync ↔ cherry_pick/driver import cycle.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from project_manager.stacker import git, selectors
from project_manager.stacker.models import SyncOptions, TrackedBranch
from project_manager.stacker.render import format as fmt

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


CollapseDecision = Literal["collapse"]


def check_parent_modifications(
    ctx: StackerCtx,  # noqa: ARG001  — kept on signature for parity with other gates
    tracked: TrackedBranch,
    slot_path: Path,
) -> str | None:
    """Error if the branch's history below the stack range was rewritten.

    Anchors on `last_synced_parent_commit` (the parent's tip at end of
    the last successful sync, advanced in lockstep with
    `managed_base_commit` on every finalize). On a never-synced branch
    this falls back to `managed_base_commit`, which was set at init —
    both equal `parent_head` there.

    The check runs against the branch's slot (where its HEAD actually
    lives); if the anchor isn't in the slot HEAD's ancestry, something
    outside stacker rewrote the below-the-stack-range history and the
    next sync would silently drop the change.
    """
    anchor = tracked.last_synced_parent_commit or tracked.managed_base_commit
    branch_head = git.rev_parse(slot_path, "HEAD")
    if git.is_ancestor(slot_path, anchor, branch_head):
        return None
    label = selectors.selector_for(tracked.repo_name, tracked.branch)
    return (
        f"{label}: history below the stack range was modified since last sync.\n"
        f"  Anchor commit {fmt.short(anchor)} is not an ancestor of "
        f"{fmt.short(branch_head)}.\n"
        f"  Re-run with --allow-drop-parent-modifications to drop the "
        f"out-of-band changes and replay only the branch's working commits "
        f"onto the parent."
    )


def merged_collapse_action(
    ctx: StackerCtx,
    tracked: TrackedBranch,
    slot_path: Path,
    *,
    allow_drop_merge: bool,
) -> CollapseDecision | str | None:
    """Return the merged-PR collapse decision for `tracked`.

    Result:
      - `None` — branch's PR isn't merged (or there's no cached PR
        state); sync proceeds on the normal cherry-pick path.
      - `"collapse"` — branch should be reset to `parent_tip`. Either
        the squashed effect of `managed_base..HEAD` is already on the
        parent (the safe case) or the caller passed
        `allow_drop_merge=True` to drop the working commits anyway.
      - Any other `str` — gate error message; caller should raise a
        `GitError` and abort sync.

    Squash detection uses `git merge-tree --write-tree --merge-base=...`
    to compute the would-be merge result in memory. If that tree equals
    the parent tip's tree, the changes are already merged.
    """
    pr = ctx.db.get_pr_state(tracked.repo_name, tracked.branch)
    if pr is None or pr.state != "MERGED":
        return None
    repo_path = ctx.paths.repo(tracked.parent_repo_name)
    parent_tip = git.rev_parse(repo_path, tracked.parent_branch)
    branch_head = git.rev_parse(slot_path, "HEAD")
    # Already in the collapsed state — branch HEAD equals parent tip and
    # there's nothing to do. Fall through so the caller can short-circuit
    # to the unchanged-parent "Nothing to sync" message instead of
    # logging a misleading second "Collapsed".
    if branch_head == parent_tip and tracked.managed_base_commit == parent_tip:
        return None
    parent_tree = git.tree_of(repo_path, parent_tip)
    merged_tree = git.merge_tree_write_tree(
        repo_path,
        tracked.managed_base_commit,
        parent_tip,
        branch_head,
    )
    squash_present = merged_tree is not None and merged_tree == parent_tree
    if squash_present or allow_drop_merge:
        return "collapse"
    label = selectors.selector_for(tracked.repo_name, tracked.branch)
    return (
        f"{label}: PR is merged ({pr.pr_url}) but the squashed result is "
        f"not present in {tracked.parent_branch}.\n"
        f"  Re-run with --allow-drop-merge to drop the branch's commits "
        f"and reset it to the parent."
    )


def run_branch_gates(
    ctx: StackerCtx,
    tracked: TrackedBranch,
    slot_path: Path,
    *,
    options: SyncOptions,
) -> None:
    """Run all pre-cherry-pick gates for one branch.

    Raises `GitError` on a gate failure that the caller hasn't opted out
    of with the matching `--allow-drop-*` flag. Successful return means
    the caller may proceed to the collapse check and then the cherry-pick
    plan. Shared between `_sync_one` and the downstream-queue advance,
    so a multi-branch sync gets the same per-branch checks.
    """
    err = check_parent_modifications(ctx, tracked, slot_path)
    if err is not None and not options.allow_drop_parent_modifications:
        raise git.GitError(err)
    err = merged_collapse_action(
        ctx,
        tracked,
        slot_path,
        allow_drop_merge=options.allow_drop_merge,
    )
    if isinstance(err, str) and err != "collapse":
        raise git.GitError(err)


def collapse_if_merged(
    ctx: StackerCtx,
    tracked: TrackedBranch,
    slot_path: Path,
    *,
    allow_drop_merge: bool,
) -> str | None:
    """If the branch's PR is merged, reset it to parent and persist.

    Returns a human-readable "Collapsed …" message when a collapse
    happened, or None when the branch is not merged (cherry-pick path
    should proceed). Assumes the gate check has already passed — i.e.
    if the merged action returned an error string, `run_branch_gates`
    already raised.
    """
    decision = merged_collapse_action(
        ctx,
        tracked,
        slot_path,
        allow_drop_merge=allow_drop_merge,
    )
    if decision != "collapse":
        return None
    repo_path = ctx.paths.repo(tracked.parent_repo_name)
    parent_tip = git.rev_parse(repo_path, tracked.parent_branch)
    git.reset_hard(slot_path, parent_tip)
    ctx.db.upsert_branch(
        TrackedBranch(
            repo_name=tracked.repo_name,
            branch=tracked.branch,
            parent_repo_name=tracked.parent_repo_name,
            parent_branch=tracked.parent_branch,
            managed_base_commit=parent_tip,
            last_synced_parent_commit=parent_tip,
            last_clean_head=parent_tip,
        )
    )
    child_label = selectors.selector_for(tracked.repo_name, tracked.branch)
    parent_label = selectors.selector_for(tracked.parent_repo_name, tracked.parent_branch)
    return f"Collapsed {child_label} to {parent_label} at {fmt.short(parent_tip)} (merged PR)."
