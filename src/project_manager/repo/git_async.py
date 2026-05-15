"""Async mirrors of `repo.git` used by `repo.ls`.

Only the read-only helpers that `pm repo ls` fans out are duplicated
here. Sync callers (stacker ops, repo.pull, etc.) keep using the
blocking versions in `repo.git` — they're single-repo scoped and don't
benefit from an event loop.
"""

from pathlib import Path

from project_manager.subprocess_run import run_async


async def current_branch(repo: Path) -> str | None:
    result = await run_async(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "-q", "HEAD"],
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


async def is_dirty(repo: Path) -> bool:
    result = await run_async(
        ["git", "-C", str(repo), "status", "--porcelain", "--ignore-submodules=all"],
        check=False,
    )
    return bool(result.stdout.strip())


async def upstream_ref(repo: Path) -> str | None:
    result = await run_async(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


async def submodule_state(repo: Path) -> str:
    result = await run_async(
        ["git", "-C", str(repo), "submodule", "status"],
        check=False,
    )
    if result.returncode != 0:
        return "none"
    lines = [line for line in result.stdout.splitlines() if line]
    if not lines:
        return "none"
    for line in lines:
        if line[0] != " ":
            return "stale"
    return "synced"


_AHEAD_BEHIND_FIELDS = 2


async def ahead_behind(repo: Path) -> tuple[int, int] | None:
    result = await run_async(
        ["git", "-C", str(repo), "rev-list", "--left-right", "--count", "@{u}...HEAD"],
        check=False,
    )
    if result.returncode != 0:
        return None
    parts = result.stdout.split()
    if len(parts) != _AHEAD_BEHIND_FIELDS:
        return None
    behind, ahead = int(parts[0]), int(parts[1])
    return ahead, behind


async def rev_parse(repo: Path, ref: str) -> str | None:
    result = await run_async(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", ref],
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


async def branch_exists(repo: Path, branch: str) -> bool:
    result = await run_async(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
        check=False,
    )
    return result.returncode == 0


async def origin_head_branch(repo: Path) -> str | None:
    result = await run_async(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"],
        check=False,
    )
    if result.returncode != 0:
        return None
    ref = result.stdout.strip()
    prefix = "origin/"
    if ref.startswith(prefix):
        return ref[len(prefix) :] or None
    return ref or None


async def remote_url(repo: Path, remote: str = "origin") -> str | None:
    """Return the fetch URL configured for `remote`, or None."""
    result = await run_async(
        ["git", "-C", str(repo), "config", "--get", f"remote.{remote}.url"],
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


async def remote_head_sha(repo: Path, branch: str) -> str | None:
    """Ask the remote for the current tip of `branch` without fetching."""
    result = await run_async(
        ["git", "-C", str(repo), "ls-remote", "--heads", "origin", f"refs/heads/{branch}"],
        check=False,
    )
    if result.returncode != 0:
        return None
    line = result.stdout.strip().splitlines()[:1]
    if not line:
        return None
    sha = line[0].split(None, 1)[0].strip()
    return sha or None
