from dataclasses import dataclass
from pathlib import Path

from project_manager.errors import CommandError
from project_manager.paths import Paths
from project_manager.repo import discovery, git
from project_manager.subprocess_run import run


@dataclass(frozen=True)
class PullResult:
    repo: str
    ok: bool
    message: str


def _precheck_skip(repo_dir: Path) -> str | None:
    """Return a skip reason if the repo is not pullable, else None."""
    if git.current_branch(repo_dir) is None:
        return "skipped: detached HEAD"
    if git.is_dirty(repo_dir):
        return "skipped: dirty working tree"
    if git.upstream_ref(repo_dir) is None:
        return "skipped: no upstream"
    return None


def _pull_one(paths: Paths, repo: str) -> PullResult:
    repo_dir = paths.repo(repo)
    skip = _precheck_skip(repo_dir)
    if skip is not None:
        return PullResult(repo=repo, ok=False, message=skip)

    try:
        run(["git", "-C", str(repo_dir), "fetch", "--prune", "origin"], stream=True)
    except CommandError as e:
        return PullResult(repo=repo, ok=False, message=f"fetch failed: {e}")

    ab = git.ahead_behind(repo_dir)
    if ab is not None and ab[1] == 0:
        return PullResult(repo=repo, ok=True, message="up-to-date")

    try:
        run(["git", "-C", str(repo_dir), "merge", "--ff-only", "@{u}"], stream=True)
    except CommandError as e:
        return PullResult(repo=repo, ok=False, message=f"merge failed: {e}")

    return PullResult(repo=repo, ok=True, message="fast-forwarded")


def pull(paths: Paths, repos: list[str] | None) -> list[PullResult]:
    """Fetch + ff-only pull each canonical repo. `repos=None` means all.

    Unknown repo names are a hard ValueError before any network call.
    """
    known = set(discovery.list_repos(paths))
    selected = sorted(known) if repos is None else repos
    unknown = [r for r in selected if r not in known]
    if unknown:
        raise ValueError(f"unknown repo(s): {', '.join(unknown)}")
    return [_pull_one(paths, r) for r in selected]
