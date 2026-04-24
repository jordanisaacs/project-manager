from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from project_manager.paths import Paths
from project_manager.render import Column
from project_manager.repo import discovery, git
from project_manager.stacker.pr.config import configured_trunk


@dataclass(frozen=True)
class RepoRow:
    repo: str
    branch: str | None        # None == detached
    dirty: bool
    submodules: str           # "none" | "synced" | "stale"
    ahead: int | None         # None if no upstream or detached
    behind: int | None
    has_upstream: bool
    # Tri-state: True = local @{u} matches the remote tip (nothing to fetch);
    # False = remote has moved past @{u} (a fetch would bring new commits);
    # None = unchecked (--offline), no upstream, or ls-remote failed.
    fetch_needed: bool | None
    # The branch pm considers canonical for this repo (the stacker trunk).
    # None if we couldn't determine one. Used to flag repos sitting on a
    # feature branch when the user expects to be on trunk.
    expected_branch: str | None


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


def _remote(row: "RepoRow") -> str:
    if row.fetch_needed is None:
        return "-"
    return "fetch-needed" if row.fetch_needed else "synced"


def _remote_style(row: "RepoRow") -> str | None:
    if row.fetch_needed is True:
        return "yellow"
    return None


def _branch_style(row: "RepoRow") -> str | None:
    # Red the cell when we know the trunk and the user is elsewhere — the
    # stacker merges into this branch, so being off it is usually a
    # transient WIP state that should be visible at a glance.
    if row.expected_branch is None or row.branch is None:
        return None
    return "red" if row.branch != row.expected_branch else None


def _state(row: "RepoRow") -> str:
    return "dirty" if row.dirty else "clean"


def _submodules(row: "RepoRow") -> str:
    # `-` for repos with no submodules so the column reads uniformly next to
    # repos that do — avoids a blank cell masquerading as "synced".
    if row.submodules == "none":
        return "-"
    return row.submodules


def _submodules_style(row: "RepoRow") -> str | None:
    return "yellow" if row.submodules == "stale" else None


COLUMNS: list[Column] = [
    Column("Repo", "repo", style="blue"),
    Column("Branch", lambda r: r.branch or "-", style=_branch_style),
    Column("State", _state, style=lambda r: "red" if r.dirty else ""),
    Column("Submodules", _submodules, style=_submodules_style),
    Column("Upstream", _upstream, style=_upstream_style),
    Column("Remote", _remote, style=_remote_style),
]


def _row(
    paths: Paths,
    repo: str,
    *,
    check_remote: bool,
    configured_trunk: str | None,
) -> RepoRow:
    repo_dir = paths.repo(repo)
    branch = git.current_branch(repo_dir)
    dirty = git.is_dirty(repo_dir)
    submodules = git.submodule_state(repo_dir)
    expected_branch = _resolve_trunk(repo_dir, configured_trunk)
    if branch is None:
        return RepoRow(repo=repo, branch=None, dirty=dirty,
                       submodules=submodules,
                       ahead=None, behind=None, has_upstream=False,
                       fetch_needed=None, expected_branch=expected_branch)
    upstream = git.upstream_ref(repo_dir)
    if upstream is None:
        return RepoRow(repo=repo, branch=branch, dirty=dirty,
                       submodules=submodules,
                       ahead=None, behind=None, has_upstream=False,
                       fetch_needed=None, expected_branch=expected_branch)
    ab = git.ahead_behind(repo_dir)
    ahead, behind = ab if ab is not None else (None, None)
    fetch_needed = _check_fetch_needed(repo_dir, upstream) if check_remote else None
    return RepoRow(repo=repo, branch=branch, dirty=dirty,
                   submodules=submodules,
                   ahead=ahead, behind=behind, has_upstream=True,
                   fetch_needed=fetch_needed, expected_branch=expected_branch)


def _check_fetch_needed(repo_dir: Path, upstream: str) -> bool | None:
    """True if the remote tip differs from our local tracking ref."""
    # upstream is "origin/main"; the remote branch name is everything after
    # the first slash. Anything without a slash can't be resolved against
    # `origin`, so skip the network call.
    if "/" not in upstream:
        return None
    remote_branch = upstream.split("/", 1)[1]
    remote_sha = git.remote_head_sha(repo_dir, remote_branch)
    if remote_sha is None:
        return None
    local_sha = git.rev_parse(repo_dir, upstream)
    if local_sha is None:
        return None
    return remote_sha != local_sha


def _resolve_trunk(repo_dir: Path, configured: str | None) -> str | None:
    """Determine the branch pm considers canonical for this repo.

    Priority: stacker's pr.trunk (authoritative if set) → origin/HEAD
    (the remote's own notion of default) → local main/master probe. Falls
    through to None when none of those apply; the caller treats None as
    "can't flag off-trunk state" rather than guessing.
    """
    if configured:
        return configured
    origin_head = git.origin_head_branch(repo_dir)
    if origin_head:
        return origin_head
    for candidate in ("main", "master"):
        if git.branch_exists(repo_dir, candidate):
            return candidate
    return None


def ls(paths: Paths, *, check_remote: bool = True) -> list[RepoRow]:
    repos = discovery.list_repos(paths)
    if not repos:
        return []
    # ls-remote is a network call; fan it out so N repos cost ~1 round-trip
    # of wall time instead of N. max_workers caps threads when a user has
    # many canonical repos.
    with ThreadPoolExecutor(max_workers=min(8, len(repos))) as pool:
        return list(pool.map(
            lambda r: _row(
                paths, r,
                check_remote=check_remote,
                configured_trunk=configured_trunk(paths, r),
            ),
            repos,
        ))
