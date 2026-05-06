from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from project_manager.errors import CommandError
from project_manager.subprocess_run import run

# Re-export `run` so `git.run(...)` call sites keep working.
__all__ = ["run"]

# Historical alias: stacker uses `GitError` for both subprocess failures and
# semantic validation errors. Keep the name so existing `except git.GitError`
# and `raise git.GitError(...)` sites continue to work.
GitError = CommandError


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path
    branch: str | None


@dataclass(frozen=True)
class GitContext:
    """Filesystem-level context of the cwd: absolute repo root + worktree + branch.

    Distinct from stacker.models.RepoContext, which is pm-level (repo_name).
    """

    repo_root: Path
    worktree_path: Path
    branch: str


def git(path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(path), *args], check=check)


def current_context(cwd: Path | None = None) -> GitContext:
    path = cwd if cwd is not None else Path.cwd()
    repo_root = repo_root_for_path(path)
    worktree_path = Path(git(path, "rev-parse", "--show-toplevel").stdout.strip())
    branch = current_branch(worktree_path)
    if not branch:
        raise GitError(
            f"Detached HEAD at {worktree_path}; stacker requires a branch checkout."
        )
    return GitContext(repo_root=repo_root, worktree_path=worktree_path, branch=branch)


def repo_root_for_path(path: Path) -> Path:
    common_dir = git(path, "rev-parse", "--git-common-dir").stdout.strip()
    common_path = Path(common_dir)
    if common_dir.endswith("/.git"):
        return common_path.parent.resolve()
    return (path / common_dir).resolve().parent


def current_branch(path: Path) -> str:
    return git(path, "branch", "--show-current").stdout.strip()


def upstream_branch(path: Path) -> str | None:
    """Return `<remote>/<remote-branch-name>` for the current branch.

    Reads `branch.<current>.remote` + `.merge` from git config directly.
    Trusts the config written by `git push -u` / `git branch
    --set-upstream-to` and does not require `@{upstream}` to resolve
    against a fetched remote-tracking ref.
    """
    remote = upstream_remote_name(path)
    remote_branch = upstream_branch_name(path)
    return f"{remote}/{remote_branch}" if remote and remote_branch else None


def upstream_branch_name(path: Path) -> str | None:
    """Return the branch name on the remote side.

    Derived from `branch.<current>.merge` (`refs/heads/<name>`). Returns
    None if the config key is unset.
    """
    branch = current_branch(path)
    if not branch:
        return None
    proc = git(path, "config", "--get", f"branch.{branch}.merge", check=False)
    if proc.returncode != 0:
        return None
    merge = proc.stdout.strip()
    prefix = "refs/heads/"
    return merge[len(prefix):] if merge.startswith(prefix) else None


def upstream_remote_name(path: Path) -> str | None:
    """Return the remote name the current branch pushes to (e.g. "origin")."""
    branch = current_branch(path)
    if not branch:
        return None
    proc = git(path, "config", "--get", f"branch.{branch}.remote", check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def remote_url(path: Path, remote: str) -> str | None:
    proc = git(path, "remote", "get-url", remote, check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


_GITHUB_SLUG_RE = re.compile(r"[:/]([^/:]+)/([^/:]+?)(?:\.git)?/?$")


def parse_github_slug(url: str) -> str | None:
    """Extract `owner/repo` from a git remote URL.

    Handles SSH (`git@github.com:org/repo.git`), HTTPS
    (`https://github.com/org/repo.git`), and the SSO-style SSH form
    (`org-NNNNN@github.com:org/repo.git`). Returns None on no match.
    """
    match = _GITHUB_SLUG_RE.search(url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def branch_exists(repo_root: Path, branch: str) -> bool:
    return git(repo_root, "rev-parse", "--verify", branch, check=False).returncode == 0 or git(
        repo_root, "rev-parse", "--verify", f"origin/{branch}", check=False
    ).returncode == 0


def rev_parse(path: Path, rev: str) -> str:
    return git(path, "rev-parse", rev).stdout.strip()


def merge_base(repo_root: Path, left: str, right: str) -> str:
    return git(repo_root, "merge-base", left, right).stdout.strip()


def rev_list(path: Path, revspec: str) -> list[str]:
    out = git(path, "rev-list", "--reverse", revspec).stdout.strip()
    return [line for line in out.splitlines() if line]


def rev_count(path: Path, revspec: str) -> int:
    out = git(path, "rev-list", "--count", revspec).stdout.strip()
    return int(out or "0")


def rev_list_picking(path: Path, left: str, right: str) -> list[str]:
    """Right-side commits not already patch-id-equivalent to anything on left.

    Uses git's native upstream-equivalent filter: `A...B` walks the symmetric
    difference back to the merge base, `--cherry-pick` excludes commits whose
    patch-id appears on the other side, and `--right-only` keeps only B's
    side. Mirrors what `git rebase` does by default to drop already-applied
    commits. `--reverse` matches `rev_list` so cherry-picks run oldest-first.
    """
    out = git(
        path, "rev-list", "--reverse", "--cherry-pick", "--right-only",
        "--no-merges", f"{left}...{right}",
    ).stdout.strip()
    return [line for line in out.splitlines() if line]


def log_subject_and_author(path: Path, revspec: str) -> list[tuple[str, str]]:
    out = git(path, "log", "--reverse", "--format=%s%x09%an", revspec).stdout.strip()
    entries: list[tuple[str, str]] = []
    for line in out.splitlines():
        if not line:
            continue
        subject, author = line.split("\t", 1) if "\t" in line else (line, "")
        entries.append((subject, author))
    return entries


def first_commit_title_and_body(path: Path, revspec: str) -> tuple[str, str]:
    out = git(path, "log", "--reverse", "--format=%s%x1f%b%x1e", revspec).stdout
    if not out:
        return ("", "")
    record = out.split("\x1e", 1)[0]
    if not record:
        return ("", "")
    if "\x1f" in record:
        subject, body = record.split("\x1f", 1)
    else:
        subject, body = record, ""
    return subject.strip(), body.strip()


def worktree_list(repo_root: Path) -> list[WorktreeInfo]:
    out = git(repo_root, "worktree", "list", "--porcelain").stdout
    items: list[WorktreeInfo] = []
    current_path: Path | None = None
    current_branch_name: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            if current_path is not None:
                items.append(WorktreeInfo(path=current_path, branch=current_branch_name))
            current_path = Path(line[len("worktree ") :])
            current_branch_name = None
        elif line.startswith("branch refs/heads/"):
            current_branch_name = line[len("branch refs/heads/") :]
        elif line == "" and current_path is not None:
            items.append(WorktreeInfo(path=current_path, branch=current_branch_name))
            current_path = None
            current_branch_name = None
    if current_path is not None:
        items.append(WorktreeInfo(path=current_path, branch=current_branch_name))
    return items


def has_tracked_changes(path: Path) -> bool:
    # One fsmonitor query instead of three: `git status` drives the
    # fsmonitor hook once and returns the same yes/no we need for
    # tracked-changes gating. `-uno` skips untracked-file enumeration,
    # `--ignored=no` skips the gitignore scan, and `--porcelain=v1` keeps
    # output stable for the `bool(.strip())` check. Pre-fsmonitor this
    # path was three quiet diffs because `git status` did redundant work;
    # with fsmonitor the handshake cost dominates, so one call wins.
    out = git(path, "status", "--porcelain=v1", "-uno", "--ignored=no", check=False).stdout
    return bool(out.strip())


def detach_head(path: Path) -> None:
    """`git checkout --detach HEAD` — drop the slot's branch attachment.

    Single point of truth for the "this slot is no longer bound to a
    branch" action. Callers handle the dirty-check themselves and only
    invoke this once they've decided the working state is OK to discard
    (or has nothing to discard). Used by:
      - `ops_slot.release` (return slot to pool with detached HEAD).
      - `stacker.ops.remove` (free the branch ref so `git branch -D` works).
      - `pm check --fix` for stale stacker-ops claims.
    """
    git(path, "checkout", "--detach", "HEAD")


def has_resumable_state(path: Path) -> bool:
    """True if the worktree has work that detach-HEAD would lose.

    Tracked-file modifications (staged or unstaged) and an in-progress
    cherry-pick (CHERRY_PICK_HEAD) both represent uncommitted resolution
    work — releasing the slot under either would discard it. This is
    the predicate every "release the slot when safe" call site consults
    before handing the slot back to the pool.
    """
    return has_tracked_changes(path) or cherry_pick_in_progress(path)


def cherry_pick_in_progress(path: Path) -> bool:
    cherry_pick_head = git(path, "rev-parse", "--git-path", "CHERRY_PICK_HEAD").stdout.strip()
    # `--git-path` returns a path relative to the repo working tree when
    # called on the primary worktree (e.g. `.git/CHERRY_PICK_HEAD`) but an
    # absolute path for linked worktrees. Resolve against `path` so the
    # existence check works in both shapes — a latent bug before absorb
    # introduced primary-worktree cherry-picks.
    resolved = Path(cherry_pick_head)
    if not resolved.is_absolute():
        resolved = path / resolved
    return resolved.exists()


def has_untracked_files(path: Path) -> bool:
    """True if there are untracked, non-ignored files in the worktree."""
    out = git(path, "ls-files", "--others", "--exclude-standard", "-z").stdout
    return bool(out)


# Ordered so the most disruptive / most common states are reported first.
_IN_PROGRESS_MARKERS: tuple[tuple[str, str], ...] = (
    ("rebase", "rebase-merge"),
    ("rebase", "rebase-apply"),
    ("cherry-pick", "CHERRY_PICK_HEAD"),
    ("merge", "MERGE_HEAD"),
    ("revert", "REVERT_HEAD"),
    ("bisect", "BISECT_LOG"),
)


def in_progress_operation(path: Path) -> str | None:
    """Return the name of an in-progress git operation, or None.

    Covers rebase (interactive + am-style), cherry-pick, merge with unresolved
    conflict, revert, and bisect. Uses `rev-parse --git-path` so the right
    directory is consulted in both main repos and worktrees.
    """
    for name, marker in _IN_PROGRESS_MARKERS:
        resolved = git(path, "rev-parse", "--git-path", marker).stdout.strip()
        if Path(resolved).exists():
            return name
    return None


def reset_hard(path: Path, target: str) -> None:
    git(path, "reset", "--hard", target)


def cherry_pick(path: Path, commit: str) -> subprocess.CompletedProcess[str]:
    return run(
        ["git", "-C", str(path), "-c", "core.editor=true", "cherry-pick", "--no-edit", commit],
        env={"GIT_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no"},
        check=False,
    )


def cherry_pick_continue(path: Path) -> subprocess.CompletedProcess[str]:
    return run(
        ["git", "-C", str(path), "-c", "core.editor=true", "cherry-pick", "--continue"],
        env={"GIT_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no"},
        check=False,
    )


def cherry_pick_skip(path: Path) -> subprocess.CompletedProcess[str]:
    return run(
        ["git", "-C", str(path), "-c", "core.editor=true", "cherry-pick", "--skip"],
        env={"GIT_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no"},
        check=False,
    )


def cherry_pick_abort(path: Path) -> subprocess.CompletedProcess[str]:
    return git(path, "cherry-pick", "--abort", check=False)


def pp_force(path: Path) -> subprocess.CompletedProcess[str]:
    """Run `git pp --force` with live output.

    Streamed so the user sees push progress, credential prompts, and
    network errors in real time instead of waiting silently for the whole
    push to finish.
    """
    return run(["git", "-C", str(path), "pp", "--force"], check=False, stream=True)


def is_ancestor(repo_root: Path, older: str, newer: str) -> bool:
    return git(repo_root, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def guess_trunk_branch(repo_root: Path) -> str:
    for candidate in ("main", "master"):
        if branch_exists(repo_root, candidate):
            return candidate
    current = current_branch(repo_root)
    if current:
        return current
    proc = git(
        repo_root, "for-each-ref", "--format=%(refname:short)", "refs/heads", check=False
    )
    branches = [line for line in proc.stdout.strip().splitlines() if line]
    if branches:
        return branches[0]
    raise GitError(f"Could not determine a trunk branch for {repo_root}.")


class GitClient(Protocol):
    """Injected seam over stacker.git module-level helpers.

    Mirrors PRBackend: the production impl (SubprocessGitClient) delegates to
    the module-level functions that shell out to `git`; tests can supply a
    fake to avoid subprocess work.
    """

    def run(
        self, path: Path, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]: ...
    def current_branch(self, path: Path) -> str: ...
    def current_context(self, cwd: Path | None = None) -> GitContext: ...
    def rev_parse(self, path: Path, rev: str) -> str: ...
    def merge_base(self, repo_root: Path, left: str, right: str) -> str: ...
    def rev_list(self, path: Path, revspec: str) -> list[str]: ...
    def rev_count(self, path: Path, revspec: str) -> int: ...
    def branch_exists(self, repo_root: Path, branch: str) -> bool: ...
    def worktree_list(self, repo_root: Path) -> list[WorktreeInfo]: ...
    def has_tracked_changes(self, path: Path) -> bool: ...
    def cherry_pick_in_progress(self, path: Path) -> bool: ...
    def reset_hard(self, path: Path, target: str) -> None: ...
    def cherry_pick(self, path: Path, commit: str) -> subprocess.CompletedProcess[str]: ...
    def cherry_pick_continue(self, path: Path) -> subprocess.CompletedProcess[str]: ...
    def cherry_pick_skip(self, path: Path) -> subprocess.CompletedProcess[str]: ...
    def cherry_pick_abort(self, path: Path) -> subprocess.CompletedProcess[str]: ...
    def pp_force(self, path: Path) -> subprocess.CompletedProcess[str]: ...
    def upstream_branch(self, path: Path) -> str | None: ...
    def upstream_branch_name(self, path: Path) -> str | None: ...
    def upstream_remote_name(self, path: Path) -> str | None: ...
    def remote_url(self, path: Path, remote: str) -> str | None: ...
    def log_subject_and_author(
        self, path: Path, revspec: str
    ) -> list[tuple[str, str]]: ...
    def first_commit_title_and_body(
        self, path: Path, revspec: str
    ) -> tuple[str, str]: ...
    def guess_trunk_branch(self, repo_root: Path) -> str: ...


class SubprocessGitClient:
    """Production GitClient impl: delegates to module-level helpers."""

    def run(
        self, path: Path, *args: str, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        return git(path, *args, check=check)

    def current_branch(self, path: Path) -> str:
        return current_branch(path)

    def current_context(self, cwd: Path | None = None) -> GitContext:
        return current_context(cwd)

    def rev_parse(self, path: Path, rev: str) -> str:
        return rev_parse(path, rev)

    def merge_base(self, repo_root: Path, left: str, right: str) -> str:
        return merge_base(repo_root, left, right)

    def rev_list(self, path: Path, revspec: str) -> list[str]:
        return rev_list(path, revspec)

    def rev_count(self, path: Path, revspec: str) -> int:
        return rev_count(path, revspec)

    def branch_exists(self, repo_root: Path, branch: str) -> bool:
        return branch_exists(repo_root, branch)

    def worktree_list(self, repo_root: Path) -> list[WorktreeInfo]:
        return worktree_list(repo_root)

    def has_tracked_changes(self, path: Path) -> bool:
        return has_tracked_changes(path)

    def cherry_pick_in_progress(self, path: Path) -> bool:
        return cherry_pick_in_progress(path)

    def reset_hard(self, path: Path, target: str) -> None:
        reset_hard(path, target)

    def cherry_pick(
        self, path: Path, commit: str
    ) -> subprocess.CompletedProcess[str]:
        return cherry_pick(path, commit)

    def cherry_pick_continue(self, path: Path) -> subprocess.CompletedProcess[str]:
        return cherry_pick_continue(path)

    def cherry_pick_skip(self, path: Path) -> subprocess.CompletedProcess[str]:
        return cherry_pick_skip(path)

    def cherry_pick_abort(self, path: Path) -> subprocess.CompletedProcess[str]:
        return cherry_pick_abort(path)

    def pp_force(self, path: Path) -> subprocess.CompletedProcess[str]:
        return pp_force(path)

    def upstream_branch(self, path: Path) -> str | None:
        return upstream_branch(path)

    def upstream_branch_name(self, path: Path) -> str | None:
        return upstream_branch_name(path)

    def upstream_remote_name(self, path: Path) -> str | None:
        return upstream_remote_name(path)

    def remote_url(self, path: Path, remote: str) -> str | None:
        return remote_url(path, remote)

    def log_subject_and_author(
        self, path: Path, revspec: str
    ) -> list[tuple[str, str]]:
        return log_subject_and_author(path, revspec)

    def first_commit_title_and_body(
        self, path: Path, revspec: str
    ) -> tuple[str, str]:
        return first_commit_title_and_body(path, revspec)

    def guess_trunk_branch(self, repo_root: Path) -> str:
        return guess_trunk_branch(repo_root)


