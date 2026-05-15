from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

PRMode = Literal["pr-pr", "repo-pr"]
Scope = Literal["current", "all"]
Details = Literal["none", "status", "status-counts", "all"]
ColorMode = Literal["off", "icon", "title", "full"]
MergedStyle = Literal["dimmed", "strikethrough", "normal"]


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


@dataclass(frozen=True)
class SyncOptions:
    """Runtime knobs for `sync` and `reparent`'s downstream sync.

    Bundles the safety-gate opt-outs and the offline switch so the
    ops/service/CLI layers don't grow ever-longer kwarg lists.
    """

    allow_drop_parent_modifications: bool = False
    allow_drop_merge: bool = False
    offline: bool = False


# Frozen singletons for default arguments: dataclasses are immutable, so
# sharing one instance across call sites is safe and avoids B008.
DEFAULT_SCOPE = ScopeSpec()
DEFAULT_PUSH_OPTIONS = PushOptions()
DEFAULT_SYNC_OPTIONS = SyncOptions()


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


@dataclass(frozen=True)
class PRState:
    """Cached GitHub PR metadata for a tracked branch.

    Source of truth for everything the renderer and `pr/find` used to
    read off `TrackedBranch.pr_url`. Stage 5 fills the review fields
    (`is_approved`, `has_open_comments`) from a bulk GraphQL call; Stage
    2 only knows `state` / `is_draft` / `merged` from the REST view.
    """

    repo_name: str
    branch: str
    pr_url: str
    state: str  # "OPEN" | "MERGED" | "CLOSED"
    pr_number: int | None = None
    is_draft: bool = False
    merged: bool = False
    merged_at: str | None = None
    is_approved: bool = False
    has_open_comments: bool = False
    fetched_at: str | None = None


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
    allow_drop_parent_modifications: bool = False
    allow_drop_merge: bool = False
