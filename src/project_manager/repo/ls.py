import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path

from project_manager.paths import Paths
from project_manager.render import Column
from project_manager.repo import discovery, git_async
from project_manager.stacker.pr.config import configured_trunks
from project_manager.subprocess_run import run_async


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


async def _row(
    paths: Paths,
    repo: str,
    *,
    check_remote: bool,
    configured_trunk: str | None,
) -> RepoRow:
    repo_dir = paths.repo(repo)
    # Every git call for this repo runs concurrently. The fetch-needed task
    # awaits the upstream-ref task internally, so the one real dependency
    # doesn't stage anything behind the slow calls (on a big repo `is_dirty`
    # can take ~1s while `upstream_ref` is ~2ms — we don't want ls-remote
    # waiting on is_dirty).
    async with asyncio.TaskGroup() as tg:
        t_branch = tg.create_task(git_async.current_branch(repo_dir))
        t_dirty = tg.create_task(git_async.is_dirty(repo_dir))
        t_subs = tg.create_task(git_async.submodule_state(repo_dir))
        t_trunk = tg.create_task(_resolve_trunk(repo_dir, configured_trunk))
        t_upstream = tg.create_task(git_async.upstream_ref(repo_dir))
        t_ab = tg.create_task(git_async.ahead_behind(repo_dir))
        t_fn = (
            tg.create_task(_check_fetch_needed(repo_dir, t_upstream))
            if check_remote else None
        )
    branch = t_branch.result()
    upstream = t_upstream.result()
    has_upstream = branch is not None and upstream is not None
    ab = t_ab.result() if has_upstream else None
    ahead, behind = ab if ab is not None else (None, None)
    fetch_needed = t_fn.result() if (t_fn is not None and has_upstream) else None
    return RepoRow(
        repo=repo,
        branch=branch,
        dirty=t_dirty.result(),
        submodules=t_subs.result(),
        ahead=ahead,
        behind=behind,
        has_upstream=has_upstream,
        fetch_needed=fetch_needed,
        expected_branch=t_trunk.result(),
    )


async def _check_fetch_needed(
    repo_dir: Path,
    upstream_task: asyncio.Task[str | None],
) -> bool | None:
    """True if the remote tip differs from our local tracking ref.

    Takes the upstream-ref *task* rather than its value so the caller can
    kick this off in the same TaskGroup as everything else — we only block
    on upstream when we actually need the branch name to build the refspec.
    """
    upstream = await upstream_task
    # upstream is "origin/main"; the remote branch name is everything after
    # the first slash. Anything without a slash can't be resolved against
    # `origin`, so skip the network call.
    if upstream is None or "/" not in upstream:
        return None
    remote_branch = upstream.split("/", 1)[1]
    remote_sha, local_sha = await asyncio.gather(
        _remote_head_sha(repo_dir, remote_branch),
        git_async.rev_parse(repo_dir, upstream),
    )
    if remote_sha is None or local_sha is None:
        return None
    return remote_sha != local_sha


# Matches remote URLs pinned to github.com (any scheme / any SSH user,
# including the `org-NNNN@github.com:` SSO-style form some hosts hand
# out). Captures owner + repo name; tolerates an optional `.git` suffix
# and trailing slash.
_GITHUB_REMOTE_RE = re.compile(
    r"""^
    (?:
        [^@/:]+@github\.com:           # git@github.com:  or  org-NNN@github.com:
      | ssh://(?:[^@/]+@)?github\.com/ # ssh://git@github.com/
      | https?://github\.com/          # https://github.com/
    )
    ([^/]+)/(.+?)                      # owner / repo
    (?:\.git)?/?$
    """,
    re.VERBOSE,
)


def _parse_github_slug(url: str) -> tuple[str, str] | None:
    match = _GITHUB_REMOTE_RE.match(url)
    return (match.group(1), match.group(2)) if match else None


async def _gh_head_sha(owner: str, repo: str, branch: str) -> str | None:
    """Resolve `refs/heads/<branch>` via the GitHub API.

    On a repo with hundreds of thousands of branches, `git ls-remote` is
    dominated by the server-side ref advertisement (protocol v2 sends
    `ref-prefix refs/heads/` — the CLI has no way to narrow further). The
    REST API does an indexed lookup instead and is typically ~5x faster.
    """
    try:
        result = await run_async(
            ["gh", "api", f"repos/{owner}/{repo}/git/refs/heads/{branch}"],
            check=False,
        )
    except FileNotFoundError:
        # gh isn't installed; caller will fall back to ls-remote.
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    obj = data.get("object") if isinstance(data, dict) else None
    if not isinstance(obj, dict):
        return None
    sha = obj.get("sha")
    return sha if isinstance(sha, str) and sha else None


async def _remote_head_sha(repo_dir: Path, branch: str) -> str | None:
    """Return the remote tip SHA for `branch`, preferring the GitHub API."""
    url = await git_async.remote_url(repo_dir)
    if url is not None:
        slug = _parse_github_slug(url)
        if slug is not None:
            owner, repo = slug
            sha = await _gh_head_sha(owner, repo, branch)
            if sha is not None:
                return sha
    # Non-GitHub remote, gh unavailable, or API call failed — ask git.
    return await git_async.remote_head_sha(repo_dir, branch)


async def _resolve_trunk(repo_dir: Path, configured: str | None) -> str | None:
    """Determine the branch pm considers canonical for this repo.

    Priority: stacker's pr.trunk (authoritative if set) → origin/HEAD
    (the remote's own notion of default) → local main/master probe. Falls
    through to None when none of those apply; the caller treats None as
    "can't flag off-trunk state" rather than guessing.
    """
    if configured:
        return configured
    origin_head = await git_async.origin_head_branch(repo_dir)
    if origin_head:
        return origin_head
    main_exists, master_exists = await asyncio.gather(
        git_async.branch_exists(repo_dir, "main"),
        git_async.branch_exists(repo_dir, "master"),
    )
    if main_exists:
        return "main"
    if master_exists:
        return "master"
    return None


async def _ls_async(paths: Paths, *, check_remote: bool) -> list[RepoRow]:
    repos = discovery.list_repos(paths)
    if not repos:
        return []
    # One SQLite read for every repo's pr.trunk instead of one per repo.
    trunks = configured_trunks(paths)
    async with asyncio.TaskGroup() as tg:
        tasks = [
            tg.create_task(
                _row(paths, r,
                     check_remote=check_remote,
                     configured_trunk=trunks.get(r)),
            )
            for r in repos
        ]
    return [t.result() for t in tasks]


def ls(paths: Paths, *, check_remote: bool = True) -> list[RepoRow]:
    return asyncio.run(_ls_async(paths, check_remote=check_remote))
