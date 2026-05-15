from __future__ import annotations

from typing import TYPE_CHECKING

from project_manager.stacker import locate, selectors
from project_manager.stacker.models import SelectorTarget
from project_manager.stacker.ops.track import require_tracked

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def status_text(ctx: StackerCtx, target: SelectorTarget) -> str:
    tracked = ctx.db.get_branch(target.repo_name, target.branch)
    op = ctx.db.get_operation(target.repo_name)
    slot_path = locate.locate_worktree(ctx.paths, target.repo_name, target.branch)
    lines = [selectors.selector_for(target.repo_name, target.branch)]
    lines.append(f"checked out in: {slot_path or '-'}")
    if tracked:
        parent_label = selectors.selector_for(tracked.parent_repo_name, tracked.parent_branch)
        lines.extend(
            [
                f"parent: {parent_label}",
                f"managed base: {tracked.managed_base_commit}",
                f"last synced parent: {tracked.last_synced_parent_commit or '-'}",
                f"last clean head: {tracked.last_clean_head or '-'}",
            ]
        )
    else:
        children = ctx.db.get_children(target.repo_name, target.branch)
        if children:
            lines.append(f"untracked root with {len(children)} tracked child(ren)")
        else:
            lines.append("not tracked")
    if op:
        lines.append(f"operation: {op.op_type} ({op.status})")
        if op.branch:
            lines.append(f"active branch: {op.branch}")
        if op.error_message:
            lines.append(f"last error: {op.error_message}")
    else:
        lines.append("operation: none")
    return "\n".join(lines)


def parent_text(ctx: StackerCtx, target: SelectorTarget) -> str:
    tracked = require_tracked(ctx, target)
    return tracked.managed_base_commit
