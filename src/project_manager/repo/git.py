"""Small read-only git helpers shared by repo.ls and repo.pull."""

from pathlib import Path

from project_manager.subprocess_run import run


def head_sha(repo: Path) -> str | None:
    """Return HEAD's object ID, or None if HEAD is unresolvable (e.g. empty repo)."""
    result = run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def current_branch(repo: Path) -> str | None:
    """Return the current branch name, or None if HEAD is detached."""
    result = run(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "-q", "HEAD"],
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def is_dirty(repo: Path) -> bool:
    """True if the working tree or index has any changes.

    Submodules are ignored: a stale submodule working tree (common right
    after a parent fast-forward that bumped a gitlink) must not block a
    subsequent pull of the parent.
    """
    result = run(
        ["git", "-C", str(repo), "status", "--porcelain", "--ignore-submodules=all"],
        check=False,
    )
    return bool(result.stdout.strip())


def upstream_ref(repo: Path) -> str | None:
    """Return the upstream tracking ref (e.g. 'origin/main'), or None."""
    result = run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def submodule_state(repo: Path) -> str:
    """Return 'none', 'synced', or 'stale' for the repo's submodules.

    `git submodule status` prefixes each line with a single char:
      ' '  → checked-out SHA matches the recorded gitlink
      '-'  → submodule not initialized
      '+'  → checked-out SHA differs from the recorded gitlink
      'U'  → submodule has merge conflicts
    Anything other than ' ' means the submodule working tree is out of
    sync with what the parent expects.
    """
    result = run(
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


def submodule_update(repo: Path) -> None:
    """Recursively init and check out submodules to match the parent's gitlinks."""
    run(
        ["git", "-C", str(repo), "submodule", "update", "--init", "--recursive"],
        stream=True,
    )


_AHEAD_BEHIND_FIELDS = 2


def ahead_behind(repo: Path) -> tuple[int, int] | None:
    """Return (ahead, behind) vs. @{u}, or None if no upstream."""
    result = run(
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


def rev_parse(repo: Path, ref: str) -> str | None:
    """Resolve a ref to its SHA, or None if the ref does not resolve."""
    result = run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", ref],
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def branch_exists(repo: Path, branch: str) -> bool:
    """True if a local branch with this name exists."""
    result = run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
        check=False,
    )
    return result.returncode == 0


def origin_head_branch(repo: Path) -> str | None:
    """Branch name that `refs/remotes/origin/HEAD` points at, or None.

    Returns just the branch (e.g. `neon-main`), not the prefixed ref
    (`origin/neon-main`). Unset `origin/HEAD` is common on clones that were
    made before the symbolic ref existed — caller should treat None as
    "don't know" and fall back to other heuristics.
    """
    result = run(
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


def remote_head_sha(repo: Path, branch: str) -> str | None:
    """Ask the remote for the current tip of `branch` without fetching.

    Uses `git ls-remote` so the local object database is not mutated. Returns
    None if the remote rejects the query (offline, auth failure, deleted
    branch) — callers treat that as "unknown" rather than "up-to-date".
    """
    result = run(
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
