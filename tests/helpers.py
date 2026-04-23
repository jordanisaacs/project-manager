"""Shared test helpers for building real git-backed pool slots.

`_mk_pool` in older tests just `mkdir`s slot paths — that was enough when
attach/detach never touched git. The branch-integration flow runs real git
commands (`status`, `checkout --detach`, etc.) against slots, so tests that
exercise detach/attach need proper worktrees.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from project_manager.paths import Paths
from project_manager.pool import add as pool_add
from project_manager.pool.slot import Slot


def init_main_repo(path: Path, branch: str = "main") -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "test"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "commit.gpgsign", "false"], check=True,
    )
    (path / "README.md").write_text("# test\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(
        [
            "git", "-C", str(path), "-c", "core.hooksPath=/dev/null",
            "commit", "-q", "-m", "init",
        ],
        check=True,
    )


def git_pool(
    paths: Paths, repo: str, n: int = 1, branch: str = "main",
) -> list[Slot]:
    """Ensure `paths.repo(repo)` is a real git repo and mint `n` worktree slots."""
    main = paths.repo(repo)
    if not main.exists():
        main.mkdir()
        init_main_repo(main, branch)
    return [pool_add.add(paths, repo) for _ in range(n)]


def git_in_slot(slot_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(slot_path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def head_ref(slot_path: Path) -> str | None:
    """Branch name if slot is on a branch, else None for detached HEAD."""
    out = subprocess.run(
        ["git", "-C", str(slot_path), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return out or None


def write_git_sentinel(slot_path: Path, name: str) -> Path:
    """Synthesize an in-progress op sentinel inside the slot's git-dir.

    Uses `git rev-parse --git-path <name>` so main-repo and worktree layouts
    both resolve correctly. Writes a file (or creates a dir for rebase-*).
    Returns the resolved path.
    """
    resolved = subprocess.run(
        ["git", "-C", str(slot_path), "rev-parse", "--git-path", name],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    p = Path(resolved)
    if not p.is_absolute():
        p = slot_path / p
    if name in ("rebase-merge", "rebase-apply"):
        p.mkdir(parents=True, exist_ok=True)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("0" * 40 + "\n")
    return p
