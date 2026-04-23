from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

PRMode = Literal["pr-pr", "repo-pr"]
Scope = Literal["current", "all"]
Details = Literal["none", "status", "status-counts", "all"]


@dataclass(frozen=True)
class ScopeSpec:
    """Scope + skip flags shared by sync / push / ls walks."""

    scope: Scope = "current"
    skip_ancestors: bool = False
    skip_descendants: bool = False
    only: bool = False
    from_branch: str | None = None


@dataclass(frozen=True)
class PushOptions:
    """Options for `push`: scope walk plus draft / create-PR behavior."""

    scope: ScopeSpec = field(default_factory=ScopeSpec)
    draft: bool = False
    publish: bool = False
    create_pr: bool = True


# Frozen singletons for default arguments: dataclasses are immutable, so
# sharing one instance across call sites is safe and avoids B008.
DEFAULT_SCOPE = ScopeSpec()
DEFAULT_PUSH_OPTIONS = PushOptions()


@dataclass(frozen=True)
class WorktreeInit:
    """Inputs for seeding a pool slot with a branch (creating or adopting)."""

    repo_name: str
    worktree_path: Path
    branch: str
    parent: ParentLocator | None = None
    copy_from: str | None = None


@dataclass(frozen=True)
class RepoContext:
    repo_name: str
    worktree_path: Path
    branch: str


@dataclass(frozen=True)
class SelectorTarget:
    repo_name: str
    branch: str


@dataclass(frozen=True)
class ParentLocator:
    repo_name: str
    branch: str


@dataclass(frozen=True)
class TrackedBranch:
    repo_name: str
    branch: str
    parent_repo_name: str
    parent_branch: str
    managed_base_commit: str
    last_synced_parent_commit: str | None
    last_clean_head: str | None
    # URL of the PR we opened for this branch, cached so we don't re-query
    # GitHub on every run. Kept across state transitions — if the PR gets
    # closed/merged externally, the URL stays and the stack block surfaces
    # it with a "[merged]" label (matching universe gitstack's UX).
    pr_url: str | None = None


@dataclass(frozen=True)
class RepoPRConfig:
    repo_name: str
    mode: PRMode
    trunk_branch: str
    target_repo: str


@dataclass
class OperationState:
    repo_name: str
    op_type: str
    status: str
    branch: str | None = None
    parent_branch: str | None = None
    root_branch: str | None = None
    queue: list[str] = field(default_factory=list)
    current_index: int = 0
    start_head: str | None = None
    target_parent_head: str | None = None
    commit_list: list[str] = field(default_factory=list)
    next_commit_index: int = 0
    error_message: str | None = None
