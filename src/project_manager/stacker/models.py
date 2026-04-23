from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


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
class RepoPRConfig:
    repo_name: str
    mode: str
    trunk_branch: str
    main_repo: str | None


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
