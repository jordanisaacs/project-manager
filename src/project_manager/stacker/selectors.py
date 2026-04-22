from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import git
from .models import ParentLocator, RepoContext, SelectorTarget


@dataclass(frozen=True)
class ParsedSelector:
    repo_query: str | None
    branch: str | None
    is_root: bool


def parse_selector(text: str) -> ParsedSelector:
    if ":" in text:
        repo_query, branch = text.split(":", 1)
        return ParsedSelector(repo_query=repo_query, branch=branch or None, is_root=branch == "")
    return ParsedSelector(repo_query=None, branch=text, is_root=False)


def discover_repos() -> dict[str, str]:
    command = _repo_scan_command()
    if command is None:
        return {}
    output = git.run(command).stdout
    repos: dict[str, str] = {}
    for line in output.splitlines():
        if "|" not in line:
            continue
        name, path = line.split("|", 1)
        repos[name] = path
    return repos


def resolve_repo_path(repo_query: str, current_repo_root: str | None = None) -> str:
    if current_repo_root and repo_query in ("", "."):
        return current_repo_root
    repo_query_path = Path(repo_query).expanduser()
    if repo_query_path.exists():
        return str(repo_query_path.resolve())
    repos = discover_repos()
    if repo_query in repos:
        return repos[repo_query]
    for name, path in repos.items():
        if Path(path).name == repo_query or path.endswith(f"/{repo_query}"):
            return path
    raise git.GitError(f"Repository '{repo_query}' not found.")


def repo_display_name(repo_root: str, *, resolve: bool = False) -> str:
    if resolve:
        try:
            repos = discover_repos()
        except git.GitError:
            repos = {}
        for name, path in repos.items():
            if os.path.realpath(path) == os.path.realpath(repo_root):
                return name
    return Path(repo_root).name


def selector_for(repo_root: str, branch: str, *, resolve: bool = False) -> str:
    return f"{repo_display_name(repo_root, resolve=resolve)}:{branch}"


def resolve_current() -> RepoContext:
    return git.current_context()


def resolve_target(
    selector: str | None,
    *,
    allow_branch_without_worktree: bool = False,
    require_branch: bool = True,
) -> SelectorTarget:
    current = git.current_context() if selector is None or ":" not in selector else _current_if_available()
    if selector is None:
        return SelectorTarget(
            repo_root=current.repo_root,
            worktree_path=current.worktree_path,
            branch=current.branch,
        )
    parsed = parse_selector(selector)
    repo_root = current.repo_root if parsed.repo_query is None and current else None
    if parsed.repo_query is not None:
        repo_root = resolve_repo_path(parsed.repo_query, current.repo_root if current else None)
    if repo_root is None:
        raise git.GitError("Not in a git repository; use [repo:]<branch>.")
    if parsed.is_root:
        branch = git.current_branch(repo_root)
        if not branch:
            raise git.GitError(f"Repository root at {repo_root} is not on a branch.")
        return SelectorTarget(repo_root=repo_root, worktree_path=repo_root, branch=branch)
    if require_branch and not parsed.branch:
        raise git.GitError("Branch name is required.")
    assert parsed.branch
    return _resolve_branch_target(
        repo_root=repo_root,
        branch=parsed.branch,
        current=current,
        allow_branch_without_worktree=allow_branch_without_worktree,
    )


def resolve_parent_for_base(repo_root: str, base_branch: str, current: RepoContext | None) -> ParentLocator:
    parsed = parse_selector(base_branch)
    if parsed.is_root or not parsed.branch:
        raise git.GitError("Base branch name is required.")
    parent_repo_root = repo_root
    if parsed.repo_query is not None:
        parent_repo_root = resolve_repo_path(parsed.repo_query, current.repo_root if current else None)
        if os.path.realpath(parent_repo_root) != os.path.realpath(repo_root):
            raise git.GitError("Parent branch for -B/--base must be in the same repo as the target branch.")
    target = _resolve_branch_target(
        repo_root=parent_repo_root,
        branch=parsed.branch,
        current=current,
        allow_branch_without_worktree=True,
    )
    return ParentLocator(
        repo_root=target.repo_root,
        worktree_path=target.worktree_path,
        branch=target.branch,
    )


def _resolve_branch_target(
    *,
    repo_root: str,
    branch: str,
    current: RepoContext | None,
    allow_branch_without_worktree: bool,
) -> SelectorTarget:
    if current and current.repo_root == repo_root and current.branch == branch:
        return SelectorTarget(repo_root=repo_root, worktree_path=current.worktree_path, branch=branch)
    for info in git.worktree_list(repo_root):
        if info.branch == branch:
            return SelectorTarget(repo_root=repo_root, worktree_path=info.path, branch=branch)
    if allow_branch_without_worktree:
        if not git.branch_exists(repo_root, branch):
            raise git.GitError(f"Branch '{branch}' not found in {repo_root}.")
        return SelectorTarget(repo_root=repo_root, worktree_path=repo_root, branch=branch)
    raise git.GitError(f"Worktree for branch '{branch}' not found in {repo_root}.")

def _current_if_available() -> RepoContext | None:
    try:
        return git.current_context()
    except Exception:
        return None


def _repo_scan_command() -> list[str] | None:
    if repo_scan := os.environ.get("STACKER_REPO_SCAN"):
        return [repo_scan]
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / "repo-scan"
        if candidate.exists():
            return [str(candidate), os.path.expanduser("~"), os.environ.get("WT_MAX_DEPTH", "999")]
    return None
