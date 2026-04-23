from dataclasses import dataclass

from project_manager.paths import Paths
from project_manager.repo import discovery, git


@dataclass(frozen=True)
class RepoRow:
    repo: str
    branch: str | None        # None == detached
    dirty: bool
    ahead: int | None         # None if no upstream or detached
    behind: int | None
    has_upstream: bool


def _row(paths: Paths, repo: str) -> RepoRow:
    repo_dir = paths.repo(repo)
    branch = git.current_branch(repo_dir)
    dirty = git.is_dirty(repo_dir)
    if branch is None:
        return RepoRow(repo=repo, branch=None, dirty=dirty,
                       ahead=None, behind=None, has_upstream=False)
    upstream = git.upstream_ref(repo_dir)
    if upstream is None:
        return RepoRow(repo=repo, branch=branch, dirty=dirty,
                       ahead=None, behind=None, has_upstream=False)
    ab = git.ahead_behind(repo_dir)
    if ab is None:
        return RepoRow(repo=repo, branch=branch, dirty=dirty,
                       ahead=None, behind=None, has_upstream=True)
    ahead, behind = ab
    return RepoRow(repo=repo, branch=branch, dirty=dirty,
                   ahead=ahead, behind=behind, has_upstream=True)


def ls(paths: Paths) -> list[RepoRow]:
    return [_row(paths, r) for r in discovery.list_repos(paths)]
