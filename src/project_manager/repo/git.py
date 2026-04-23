"""Small read-only git helpers shared by repo.ls and repo.pull."""

from pathlib import Path

from project_manager.subprocess_run import run


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
    """True if the working tree or index has any changes."""
    result = run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=False,
    )
    return bool(result.stdout.strip())


def upstream_ref(repo: Path) -> str | None:
    """Return the upstream tracking ref (e.g. 'origin/main'), or None."""
    result = run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref",
         "--symbolic-full-name", "@{u}"],
        check=False,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


_AHEAD_BEHIND_FIELDS = 2


def ahead_behind(repo: Path) -> tuple[int, int] | None:
    """Return (ahead, behind) vs. @{u}, or None if no upstream."""
    result = run(
        ["git", "-C", str(repo), "rev-list", "--left-right", "--count",
         "@{u}...HEAD"],
        check=False,
    )
    if result.returncode != 0:
        return None
    parts = result.stdout.split()
    if len(parts) != _AHEAD_BEHIND_FIELDS:
        return None
    behind, ahead = int(parts[0]), int(parts[1])
    return ahead, behind
