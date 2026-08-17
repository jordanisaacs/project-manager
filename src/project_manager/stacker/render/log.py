from __future__ import annotations

from typing import TYPE_CHECKING

from project_manager.stacker import git, selectors
from project_manager.stacker.models import SelectorTarget
from project_manager.stacker.ops.track import require_tracked

from . import format as fmt

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


def log_text(ctx: StackerCtx, target: SelectorTarget) -> str:
    tracked = require_tracked(ctx, target)
    # Log refs directly from the canonical repo. Rendering history is
    # read-only and must not require (or disturb) a branch checkout.
    repo_path = ctx.paths.repo(target.repo_name)
    entries = git.log_subject_and_author(
        repo_path,
        f"{tracked.managed_base_commit}..{target.branch}",
    )
    if not entries:
        return (
            f"No commits since "
            f"{selectors.selector_for(tracked.parent_repo_name, tracked.parent_branch)} "
            f"at {fmt.short(tracked.managed_base_commit)}."
        )
    label = selectors.selector_for(tracked.repo_name, tracked.branch)
    lines = [f"{label} commits since parent:"]
    for subject, author in entries:
        lines.append(f"{subject} ({author})" if author else subject)
    return "\n".join(lines)
