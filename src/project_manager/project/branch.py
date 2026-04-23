"""Branch save/restore/park helpers for project attach/detach.

Thin composition over `stacker.git` (current_branch, has_tracked_changes,
worktree_list, branch_exists, git) and `pool.worktree.default_branch`. Lives
alongside other project code so attach/detach can read cleanly without
reaching across modules.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path

from project_manager.errors import ProjectError
from project_manager.pool import worktree as wt
from project_manager.stacker import git


class RestoreResult(enum.Enum):
    RESTORED = "restored"
    SKIPPED_IN_USE = "skipped_in_use"
    SKIPPED_MISSING_BRANCH = "skipped_missing_branch"


@dataclass(frozen=True)
class RestoreOutcome:
    result: RestoreResult
    branch: str
    conflict_path: Path | None = None


def ensure_clean(slot_path: Path, repo: str) -> None:
    blocker = cleanliness_blocker(slot_path)
    if blocker is not None:
        raise ProjectError(f"repo '{repo}' at {slot_path} {blocker}")


def cleanliness_blocker(slot_path: Path) -> str | None:
    """Return a human-readable reason the slot isn't detach-clean, or None.

    Shared between `ensure_clean` (raises) and planners (collect + report).
    Order: in-progress ops first, then tracked changes, then untracked files.
    """
    op = git.in_progress_operation(slot_path)
    if op is not None:
        return f"has {op} in progress; abort or complete it before detaching"
    if git.has_tracked_changes(slot_path):
        return "has uncommitted changes; commit or discard them before detaching"
    if git.has_untracked_files(slot_path):
        return (
            "has untracked files; commit, remove, or gitignore them before detaching"
        )
    return None


def read_current_branch(slot_path: Path) -> str | None:
    name = git.current_branch(slot_path)
    return name or None


def park_to_default(slot_path: Path, main_repo: Path) -> None:
    default = wt.default_branch(main_repo)
    git.git(slot_path, "checkout", "--detach", default)


def _branch_in_use_elsewhere(
    main_repo: Path, branch: str, except_path: Path,
) -> Path | None:
    except_resolved = except_path.resolve()
    for info in git.worktree_list(main_repo):
        if info.branch == branch and info.path.resolve() != except_resolved:
            return info.path
    return None


def restore(slot_path: Path, main_repo: Path, branch: str) -> RestoreOutcome:
    conflict = _branch_in_use_elsewhere(main_repo, branch, slot_path)
    if conflict is not None:
        return RestoreOutcome(RestoreResult.SKIPPED_IN_USE, branch, conflict)
    if not git.branch_exists(main_repo, branch):
        return RestoreOutcome(RestoreResult.SKIPPED_MISSING_BRANCH, branch)
    git.git(slot_path, "checkout", branch)
    return RestoreOutcome(RestoreResult.RESTORED, branch)
