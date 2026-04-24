from dataclasses import dataclass

from project_manager.paths import Paths
from project_manager.render import Column
from project_manager.repo import discovery, git


@dataclass(frozen=True)
class RepoRow:
    repo: str
    branch: str | None        # None == detached
    dirty: bool
    ahead: int | None         # None if no upstream or detached
    behind: int | None
    has_upstream: bool


def _upstream(row: "RepoRow") -> str:
    if row.branch is None:
        return "-"
    if not row.has_upstream or row.ahead is None or row.behind is None:
        return "no-upstream"
    if row.ahead == 0 and row.behind == 0:
        return "up-to-date"
    parts: list[str] = []
    if row.ahead:
        parts.append(f"ahead {row.ahead}")
    if row.behind:
        parts.append(f"behind {row.behind}")
    return " ".join(parts)


def _upstream_style(row: "RepoRow") -> str | None:
    if row.branch is None or not row.has_upstream:
        return None
    if row.ahead or row.behind:
        return "yellow"
    return None


def _state(row: "RepoRow") -> str:
    return "dirty" if row.dirty else "clean"


COLUMNS: list[Column] = [
    Column("Repo", "repo", style="blue"),
    Column("Branch", lambda r: r.branch or "-"),
    Column("State", _state, style=lambda r: "red" if r.dirty else ""),
    Column("Upstream", _upstream, style=_upstream_style),
]


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
