from __future__ import annotations

from typing import TYPE_CHECKING

from project_manager.stacker import git
from project_manager.stacker.models import ScopeSpec, SelectorTarget, TrackedBranch
from project_manager.stacker.ops.track import require_tracked

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def ancestor_chain(ctx: StackerCtx, tracked: TrackedBranch) -> list[TrackedBranch]:
    """Return the tracked ancestor chain, root-first, including `tracked`.

    Walks `parent_repo_name`/`parent_branch` upward until we hit an
    untracked parent (typically the trunk branch). Used by `pr` so that
    running `pm stacker pr <leaf>` pushes and opens PRs for every
    tracked ancestor before the leaf, in root→leaf order.
    """
    ancestors: list[TrackedBranch] = []
    cursor: TrackedBranch | None = tracked
    while cursor is not None:
        ancestors.append(cursor)
        cursor = ctx.db.get_branch(cursor.parent_repo_name, cursor.parent_branch)
    ancestors.reverse()
    return ancestors


def lineage(ctx: StackerCtx, tracked: TrackedBranch) -> list[TrackedBranch]:
    """Return the full connected component containing `tracked`, parent-before-child.

    Walks up through tracked parents to the topmost-tracked ancestor
    (the first one whose parent is untracked, typically trunk), then
    toposorts the entire subtree under that root. In a linear stack
    that's the same as `ancestors + tracked + descendants_of_tracked`;
    in a multi-arm stack the result also includes every sibling/cousin
    arm reachable through any tracked edge.

    Used as the `-c`/`--current` scope for sync/push/ls and as the
    component for PR-body rendering — every consumer wants the whole
    stack, not just the spine through the target. (`--only` is the
    escape hatch for "this branch only.")
    """
    root = tracked
    while True:
        parent = ctx.db.get_branch(root.parent_repo_name, root.parent_branch)
        if not parent:
            break
        root = parent
    return [root, *toposorted_descendants(ctx, root.repo_name, root.branch)]


def toposorted_all(ctx: StackerCtx, repo_name: str) -> list[TrackedBranch]:
    """All tracked branches in `repo_name`, ordered parent-before-child.

    Branches whose parent is not tracked (typical roots off trunk) go
    first, in stable `branch` order. Remaining branches follow a DFS
    from each root so a cascading op can rely on parents being synced
    before their children.
    """
    all_branches = ctx.db.list_branches(repo_name)
    by_parent: dict[str, list[TrackedBranch]] = {}
    tracked_names: set[str] = set()
    for b in all_branches:
        by_parent.setdefault(b.parent_branch, []).append(b)
        tracked_names.add(b.branch)
    ordered: list[TrackedBranch] = []
    visited: set[str] = set()

    def walk(parent_branch: str) -> None:
        for child in sorted(by_parent.get(parent_branch, []), key=lambda b: b.branch):
            if child.branch in visited:
                continue
            visited.add(child.branch)
            ordered.append(child)
            walk(child.branch)

    roots = sorted(
        (b for b in all_branches if b.parent_branch not in tracked_names),
        key=lambda b: (b.parent_branch, b.branch),
    )
    for root in roots:
        if root.branch in visited:
            continue
        visited.add(root.branch)
        ordered.append(root)
        walk(root.branch)
    return ordered


def toposorted_descendants(ctx: StackerCtx, repo_name: str, branch: str) -> list[TrackedBranch]:
    ordered: list[TrackedBranch] = []

    def walk(parent_branch: str) -> None:
        for child in ctx.db.get_children(repo_name, parent_branch):
            ordered.append(child)
            walk(child.branch)

    walk(branch)
    return ordered


def resolve_scope(
    ctx: StackerCtx,
    repo_name: str,
    target_branch: str,
    spec: ScopeSpec,
) -> list[TrackedBranch]:
    """Resolve scope flags into an ordered branch list.

    Order is parent-before-child, safe to sync/push sequentially:
    `lineage` (the full connected component) for scope="current",
    `toposorted_all` for scope="all". `only` short-circuits to just
    the target. `skip_ancestors` / `skip_descendants` are applied
    *structurally* — using the ancestor chain and descendant subtree
    of target — so a multi-arm component still drops the correct
    branches even though target sits between sibling arms in the
    toposort. `from_branch` drops entries before that branch in the
    resolved list.
    """
    if spec.only:
        tracked = require_tracked(ctx, SelectorTarget(repo_name=repo_name, branch=target_branch))
        return [tracked]
    if spec.scope == "all":
        resolved = toposorted_all(ctx, repo_name)
        if spec.from_branch:
            resolved = _drop_until(resolved, spec.from_branch)
        return resolved
    target = require_tracked(ctx, SelectorTarget(repo_name=repo_name, branch=target_branch))
    component = lineage(ctx, target)
    drop: set[str] = set()
    if spec.skip_ancestors:
        # ancestor_chain returns root → … → target inclusive; drop
        # everything before target (target itself stays in the walk).
        drop.update(b.branch for b in ancestor_chain(ctx, target) if b.branch != target.branch)
    if spec.skip_descendants:
        drop.update(b.branch for b in toposorted_descendants(ctx, target.repo_name, target.branch))
    resolved = [b for b in component if b.branch not in drop]
    if spec.from_branch:
        resolved = _drop_until(resolved, spec.from_branch)
    return resolved


def _drop_until(branches: list[TrackedBranch], name: str) -> list[TrackedBranch]:
    for index, item in enumerate(branches):
        if item.branch == name:
            return branches[index:]
    raise git.GitError(f"--from {name!r} is not in the resolved scope.")
