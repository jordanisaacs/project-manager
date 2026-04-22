from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RepoContext:
    repo_root: str
    worktree_path: str
    branch: str


@dataclass(frozen=True)
class SelectorTarget:
    repo_root: str
    worktree_path: str
    branch: str


@dataclass(frozen=True)
class ParentLocator:
    repo_root: str
    worktree_path: str
    branch: str


@dataclass(frozen=True)
class TrackedWorktree:
    repo_root: str
    worktree_path: str
    branch: str
    parent_repo_root: str
    parent_worktree_path: str
    parent_branch: str
    managed_base_commit: str
    last_synced_parent_commit: str | None
    last_clean_head: str | None


@dataclass(frozen=True)
class RepoPRConfig:
    repo_root: str
    mode: str
    trunk_branch: str
    main_repo: str | None


@dataclass
class OperationState:
    repo_root: str
    op_type: str
    status: str
    root_path: str | None = None
    root_branch: str | None = None
    queue: list[str] = field(default_factory=list)
    current_index: int = 0
    worktree_path: str | None = None
    worktree_branch: str | None = None
    parent_path: str | None = None
    parent_branch: str | None = None
    start_head: str | None = None
    target_parent_head: str | None = None
    commit_list: list[str] = field(default_factory=list)
    next_commit_index: int = 0
    error_message: str | None = None
