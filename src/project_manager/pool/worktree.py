import contextlib
import shutil
import subprocess
from pathlib import Path


class GitError(Exception):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise GitError(
            f"git -C {repo} {' '.join(args)} failed ({result.returncode}):\n{result.stderr}"
        )
    return result


def default_branch(repo: Path) -> str:
    """Best-effort default branch detection.

    origin/HEAD → current branch → error.
    """
    result = _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", check=False)
    if result.returncode == 0:
        ref = result.stdout.strip()
        if ref.startswith("refs/remotes/origin/"):
            return ref[len("refs/remotes/origin/") :]
    result = _git(repo, "branch", "--show-current", check=False)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise GitError(f"could not determine default branch for {repo}")


def worktree_add_detached(repo: Path, slot: Path, branch: str) -> None:
    _git(repo, "worktree", "add", "--detach", str(slot), branch)


# git ls-tree -r HEAD lines are "<mode> <type> <sha>\t<path>" — 4 whitespace-separated fields.
_LS_TREE_FIELDS = 4
_SUBMODULE_MODE = "160000"


def _submodule_entries(worktree: Path) -> list[tuple[str, str]]:
    """Return [(commit, path), ...] for every submodule pinned at HEAD."""
    result = _git(worktree, "ls-tree", "-r", "HEAD")
    entries: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < _LS_TREE_FIELDS:
            continue
        mode, _obj_type, sha, path = parts
        if mode == _SUBMODULE_MODE:
            entries.append((sha, path))
    return entries


def init_submodules(main_repo: Path, worktree: Path) -> None:
    """For each submodule pinned at HEAD in the worktree:

    git clone --reference <main_repo>/<path> --no-checkout <url> <worktree>/<path>
    git -C <worktree>/<path> checkout <sha>

    Skips if main repo has not initialized the submodule.
    """
    entries = _submodule_entries(worktree)
    for sha, path in entries:
        main_sub = main_repo / path
        worktree_sub = worktree / path
        if not (main_sub / ".git").exists():
            raise GitError(
                f"submodule '{path}' is not initialized in main repo {main_repo}; "
                f"run `git -C {main_repo} submodule update --init --recursive` first"
            )
        url = _git(main_sub, "remote", "get-url", "origin").stdout.strip()
        worktree_sub.parent.mkdir(parents=True, exist_ok=True)
        if worktree_sub.is_dir():
            with contextlib.suppress(OSError):
                worktree_sub.rmdir()
        _git(
            worktree,
            "clone",
            "--reference",
            str(main_sub),
            "--no-checkout",
            url,
            str(worktree_sub),
        )
        checkout = _git(worktree_sub, "checkout", "--quiet", sha, check=False)
        if checkout.returncode != 0:
            # try fetching the specific commit
            _git(worktree_sub, "fetch", "origin", sha, "--depth=1", check=False)
            _git(worktree_sub, "checkout", "--quiet", sha)


def copy_claude_files(main_repo: Path, worktree: Path) -> None:
    """Copy untracked CLAUDE.md / CLAUDE.local.md from main repo into the worktree."""
    for name in ("CLAUDE.md", "CLAUDE.local.md"):
        src = main_repo / name
        if not src.is_file():
            continue
        tracked = _git(main_repo, "ls-files", "--error-unmatch", name, check=False)
        if tracked.returncode == 0:
            continue  # tracked; git worktree add already put a copy in place
        shutil.copy2(src, worktree / name)


def run_init_script(main_repo: Path, worktree: Path) -> None:
    """If init.sh exists as an untracked file in main_repo, copy it; then run it if executable."""
    init_src = main_repo / "init.sh"
    init_dst = worktree / "init.sh"
    if init_src.is_file() and not init_dst.exists():
        tracked = _git(main_repo, "ls-files", "--error-unmatch", "init.sh", check=False)
        if tracked.returncode != 0:
            shutil.copy2(init_src, init_dst)
    if not init_dst.is_file():
        return
    cmd = [str(init_dst)] if init_dst.stat().st_mode & 0o111 else ["bash", str(init_dst)]
    subprocess.run(cmd, cwd=worktree, check=False)
