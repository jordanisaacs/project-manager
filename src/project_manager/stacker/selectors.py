from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from project_manager.paths import Paths

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
        return ParsedSelector(
            repo_query=repo_query, branch=branch or None, is_root=branch == ""
        )
    return ParsedSelector(repo_query=None, branch=text, is_root=False)


def resolve_repo_name(paths: Paths, repo_query: str) -> str:
    """Resolve a user-provided repo reference to a pm repo name.

    Accepts a bare pm repo name (e.g. 'demo'), a full filesystem path, or a
    path relative to paths.repos. Raises if the repo is not registered with pm.
    """
    candidate = paths.repos / repo_query
    if candidate.is_dir():
        return repo_query
    repo_path = Path(repo_query).expanduser()
    if repo_path.is_dir():
        try:
            relative = repo_path.resolve().relative_to(paths.repos.resolve())
        except ValueError as exc:
            raise git.GitError(
                f"Path {repo_query} is outside pm's repos directory ({paths.repos})."
            ) from exc
        parts = relative.parts
        if not parts:
            raise git.GitError(f"Path {repo_query} resolves to the repos root.")
        return parts[0]
    raise git.GitError(f"Repository '{repo_query}' not found under {paths.repos}.")


def selector_for(repo_name: str, branch: str) -> str:
    return f"{repo_name}:{branch}"


def resolve_target(
    paths: Paths,
    selector: str | None,
    *,
    current_repo_name: str | None = None,
    require_branch: bool = True,
) -> SelectorTarget:
    """Resolve a selector (``repo:branch`` or bare ``branch``) to a SelectorTarget.

    Bare ``branch`` requires ``current_repo_name``; otherwise raises.
    """
    if selector is None:
        raise git.GitError("A selector is required.")
    parsed = parse_selector(selector)
    repo_name = current_repo_name
    if parsed.repo_query is not None:
        repo_name = resolve_repo_name(paths, parsed.repo_query)
    if repo_name is None:
        raise git.GitError("Provide a repo via 'repo:branch' or --repo.")
    if parsed.is_root:
        branch = git.current_branch(paths.repo(repo_name))
        if not branch:
            raise git.GitError(f"Repository {repo_name} is not on a branch.")
        return SelectorTarget(repo_name=repo_name, branch=branch)
    if require_branch and not parsed.branch:
        raise git.GitError("Branch name is required.")
    assert parsed.branch
    if not paths.repo(repo_name).is_dir():
        raise git.GitError(f"Repository '{repo_name}' not found under {paths.repos}.")
    return SelectorTarget(repo_name=repo_name, branch=parsed.branch)


def resolve_parent_for_base(
    paths: Paths,
    repo_name: str,
    base_selector: str,
) -> ParentLocator:
    parsed = parse_selector(base_selector)
    if parsed.is_root or not parsed.branch:
        raise git.GitError("Base branch name is required.")
    parent_repo_name = repo_name
    if parsed.repo_query is not None:
        parent_repo_name = resolve_repo_name(paths, parsed.repo_query)
        if parent_repo_name != repo_name:
            raise git.GitError(
                "Parent branch for -B/--base must be in the same repo as the target branch."
            )
    if not git.branch_exists(paths.repo(parent_repo_name), parsed.branch):
        raise git.GitError(f"Branch '{parsed.branch}' not found in {parent_repo_name}.")
    return ParentLocator(repo_name=parent_repo_name, branch=parsed.branch)


def resolve_current(paths: Paths, cwd: Path | None = None) -> RepoContext | None:
    """Best-effort: if cwd is a pm worktree, return its context; else None."""
    try:
        ctx = git.current_context(cwd)
    except git.GitError:
        return None
    try:
        relative = ctx.worktree_path.resolve().relative_to(paths.worktrees.resolve())
    except ValueError:
        return None
    parts = relative.parts
    if not parts:
        return None
    return RepoContext(
        repo_name=parts[0], worktree_path=ctx.worktree_path, branch=ctx.branch
    )
