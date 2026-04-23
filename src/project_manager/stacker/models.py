from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

PRMode = Literal["pr-pr", "repo-pr"]


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
