from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import RepoContext


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorktreeInfo:
    path: str
    branch: str | None


def run(
    cmd: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    capture_output: bool = True,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        env=full_env,
        text=True,
        input=input_text,
        capture_output=capture_output,
        check=False,
    )
    if check and proc.returncode != 0:
        raise GitError(_format_failure(cmd, proc))
    return proc


def git(path: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", path, *args], check=check)


def current_context(cwd: str | None = None) -> RepoContext:
    path = cwd or os.getcwd()
    repo_root = repo_root_for_path(path)
    worktree_path = git(path, "rev-parse", "--show-toplevel").stdout.strip()
    branch = current_branch(worktree_path)
    if not branch:
        raise GitError(f"Detached HEAD at {worktree_path}; stacker requires a branch checkout.")
    return RepoContext(repo_root=repo_root, worktree_path=worktree_path, branch=branch)


def repo_root_for_path(path: str) -> str:
    common_dir = git(path, "rev-parse", "--git-common-dir").stdout.strip()
    if common_dir.endswith("/.git"):
        return str(Path(common_dir).parent.resolve())
    return str((Path(path) / common_dir).resolve().parent)


def current_branch(path: str) -> str:
    return git(path, "branch", "--show-current").stdout.strip()


def upstream_branch(path: str) -> str | None:
    proc = git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
    if proc.returncode != 0:
        return None
    upstream = proc.stdout.strip()
    return upstream or None


def upstream_branch_name(path: str) -> str | None:
    upstream = upstream_branch(path)
    if not upstream or "/" not in upstream:
        return None
    return upstream.split("/", 1)[1]


def branch_exists(repo_root: str, branch: str) -> bool:
    return git(repo_root, "rev-parse", "--verify", branch, check=False).returncode == 0 or git(
        repo_root, "rev-parse", "--verify", f"origin/{branch}", check=False
    ).returncode == 0


def rev_parse(path: str, rev: str) -> str:
    return git(path, "rev-parse", rev).stdout.strip()


def merge_base(repo_root: str, left: str, right: str) -> str:
    return git(repo_root, "merge-base", left, right).stdout.strip()


def rev_list(path: str, revspec: str) -> list[str]:
    out = git(path, "rev-list", "--reverse", revspec).stdout.strip()
    return [line for line in out.splitlines() if line]


def rev_count(path: str, revspec: str) -> int:
    out = git(path, "rev-list", "--count", revspec).stdout.strip()
    return int(out or "0")


def log_subject_and_author(path: str, revspec: str) -> list[tuple[str, str]]:
    out = git(path, "log", "--reverse", "--format=%s%x09%an", revspec).stdout.strip()
    entries: list[tuple[str, str]] = []
    for line in out.splitlines():
        if not line:
            continue
        subject, author = line.split("\t", 1) if "\t" in line else (line, "")
        entries.append((subject, author))
    return entries


def first_commit_title_and_body(path: str, revspec: str) -> tuple[str, str]:
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


def worktree_list(repo_root: str) -> list[WorktreeInfo]:
    out = git(repo_root, "worktree", "list", "--porcelain").stdout
    items: list[WorktreeInfo] = []
    current_path: str | None = None
    current_branch_name: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            if current_path is not None:
                items.append(WorktreeInfo(path=current_path, branch=current_branch_name))
            current_path = line[len("worktree ") :]
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


def has_tracked_changes(path: str) -> bool:
    # `git status` is expensive in large repos. Quiet diff checks are enough here
    # because stacker only needs a yes/no answer for tracked staged or unstaged changes.
    git(path, "update-index", "-q", "--refresh", check=False)
    if git(path, "rev-parse", "--verify", "HEAD", check=False).returncode != 0:
        return bool(git(path, "status", "--porcelain=v1", "-uno").stdout.strip())
    if git(path, "diff-index", "--quiet", "--cached", "HEAD", "--", check=False).returncode != 0:
        return True
    if git(path, "diff-files", "--quiet", "--", check=False).returncode != 0:
        return True
    return False


def cherry_pick_in_progress(path: str) -> bool:
    cherry_pick_head = git(path, "rev-parse", "--git-path", "CHERRY_PICK_HEAD").stdout.strip()
    return Path(cherry_pick_head).exists()


def reset_hard(path: str, target: str) -> None:
    git(path, "reset", "--hard", target)


def cherry_pick(path: str, commit: str) -> subprocess.CompletedProcess[str]:
    return run(
        ["git", "-C", path, "-c", "core.editor=true", "cherry-pick", "--no-edit", commit],
        env={"GIT_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no"},
        check=False,
    )


def cherry_pick_continue(path: str) -> subprocess.CompletedProcess[str]:
    return run(
        ["git", "-C", path, "-c", "core.editor=true", "cherry-pick", "--continue"],
        env={"GIT_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no"},
        check=False,
    )


def cherry_pick_skip(path: str) -> subprocess.CompletedProcess[str]:
    return run(
        ["git", "-C", path, "-c", "core.editor=true", "cherry-pick", "--skip"],
        env={"GIT_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no"},
        check=False,
    )


def cherry_pick_abort(path: str) -> subprocess.CompletedProcess[str]:
    return git(path, "cherry-pick", "--abort", check=False)


def pp_force(path: str) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", path, "pp", "--force"], check=False)


def is_ancestor(repo_root: str, older: str, newer: str) -> bool:
    return git(repo_root, "merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


def guess_trunk_branch(repo_root: str) -> str:
    for candidate in ("main", "master"):
        if branch_exists(repo_root, candidate):
            return candidate
    current = current_branch(repo_root)
    if current:
        return current
    out = git(repo_root, "for-each-ref", "--format=%(refname:short)", "refs/heads", check=False).stdout.strip()
    branches = [line for line in out.splitlines() if line]
    if branches:
        return branches[0]
    raise GitError(f"Could not determine a trunk branch for {repo_root}.")


def _format_failure(cmd: list[str], proc: subprocess.CompletedProcess[str]) -> str:
    pieces = ["command failed:", " ".join(cmd)]
    if proc.stdout.strip():
        pieces.append(proc.stdout.strip())
    if proc.stderr.strip():
        pieces.append(proc.stderr.strip())
    return "\n".join(pieces)
