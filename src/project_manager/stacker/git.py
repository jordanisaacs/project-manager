from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

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
    proc = git(
        path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False
    )
    if proc.returncode != 0:
        return None
    upstream = proc.stdout.strip()
    return upstream or None


def upstream_branch_name(path: Path) -> str | None:
    upstream = upstream_branch(path)
    if not upstream or "/" not in upstream:
        return None
    return upstream.split("/", 1)[1]


def upstream_remote_name(path: Path) -> str | None:
    """Return the remote name the current branch is pushed to (e.g. "origin").

    Reads `branch.<current>.remote` from git config rather than resolving
    `@{upstream}`, so this works even before `git fetch` has materialized a
    remote-tracking ref — useful immediately after a push.
    """
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
    (`https://github.com/org/repo.git`), and the custom SSH form Databricks
    uses (`org-NNNNN@github.com:org/repo.git`). Returns None on no match.
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
    # `git status` is expensive in large repos. Quiet diff checks are enough here
    # because stacker only needs a yes/no answer for tracked staged or unstaged changes.
    git(path, "update-index", "-q", "--refresh", check=False)
    if git(path, "rev-parse", "--verify", "HEAD", check=False).returncode != 0:
        return bool(git(path, "status", "--porcelain=v1", "-uno").stdout.strip())
    if git(path, "diff-index", "--quiet", "--cached", "HEAD", "--", check=False).returncode != 0:
        return True
    return git(path, "diff-files", "--quiet", "--", check=False).returncode != 0


def cherry_pick_in_progress(path: Path) -> bool:
    cherry_pick_head = git(path, "rev-parse", "--git-path", "CHERRY_PICK_HEAD").stdout.strip()
    return Path(cherry_pick_head).exists()


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


