from dataclasses import dataclass
from pathlib import Path

from project_manager.errors import CommandError
from project_manager.paths import Paths
from project_manager.render import Column
from project_manager.repo import discovery, git
from project_manager.subprocess_run import run


@dataclass(frozen=True)
class PullResult:
    repo: str
    ok: bool
    message: str


COLUMNS: list[Column] = [
    Column("Repo", "repo", style="blue"),
    Column(
        "Result",
        lambda r: "ok" if r.ok else "fail",
        style=lambda r: "green" if r.ok else "red",
    ),
    Column("Message", "message"),
]


def _precheck_skip(repo_dir: Path) -> str | None:
    """Return a skip reason if the repo is not pullable, else None."""
    if git.current_branch(repo_dir) is None:
        return "skipped: detached HEAD"
    if git.is_dirty(repo_dir):
        return "skipped: dirty working tree"
    if git.upstream_ref(repo_dir) is None:
        return "skipped: no upstream"
    return None


def _sync_submodules(repo_dir: Path, base_message: str) -> PullResult:
    """Recursively sync submodules, annotating the given result message."""
    repo = repo_dir.name
    if git.submodule_state(repo_dir) == "none":
        return PullResult(repo=repo, ok=True, message=base_message)
    try:
        git.submodule_update(repo_dir)
    except CommandError as e:
        return PullResult(
            repo=repo,
            ok=False,
            message=f"{base_message}; submodule update failed: {e}",
        )
    return PullResult(repo=repo, ok=True, message=base_message)


def _pull_one(paths: Paths, repo: str) -> PullResult:
    repo_dir = paths.repo(repo)
    skip = _precheck_skip(repo_dir)
    if skip is not None:
        return PullResult(repo=repo, ok=False, message=skip)

    head_before = git.head_sha(repo_dir)
    try:
        run(
            ["git", "-C", str(repo_dir), "pull", "--ff-only", "--prune",
             "--recurse-submodules"],
            stream=True,
        )
    except CommandError as e:
        return PullResult(repo=repo, ok=False, message=f"pull failed: {e}")

    base_message = (
        "up-to-date" if git.head_sha(repo_dir) == head_before
        else "fast-forwarded"
    )
    # `pull --recurse-submodules` updates already-populated submodules but
    # won't --init new ones; run `submodule update --init --recursive` so
    # newly-added submodules are checked out.
    return _sync_submodules(repo_dir, base_message)


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
