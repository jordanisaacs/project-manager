import asyncio
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from project_manager import config
from project_manager.errors import CommandError
from project_manager.subprocess_run import run


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", "-C", str(repo), *args], check=check)


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
    raise CommandError(f"could not determine default branch for {repo}")


def worktree_add_detached(repo: Path, slot: Path, branch: str) -> None:
    _git(repo, "worktree", "add", "--detach", str(slot), branch)


def update_submodules(worktree: Path) -> None:
    """Synchronize URLs and recursively check out the gitlinks at HEAD.

    URL synchronization matters when a branch changes `.gitmodules`; the
    recursive update handles both newly-added and nested submodules. The
    explicit checkout mode avoids inheriting a per-submodule `update`
    strategy that would leave a reusable slot on the wrong commit.
    """
    if not (worktree / ".gitmodules").is_file():
        return
    _git(worktree, "submodule", "sync", "--recursive")
    _git(worktree, "submodule", "update", "--init", "--recursive", "--checkout")


_SUBMODULE_MODE = "160000"
_LS_TREE_FIELD_COUNT = 3
_SUBMODULE_PATH_KEY = re.compile(r"^submodule\.(?P<name>.+)\.path$")
_SCP_LIKE_URL = re.compile(r"^(?:[^/@:]+@)?[^/:]+:.+")


@dataclass(frozen=True)
class _Submodule:
    name: str
    path: str
    commit: str


def _submodule_entries(worktree: Path) -> list[_Submodule]:
    """Return configured submodules and their gitlinks at the current HEAD."""
    gitmodules = worktree / ".gitmodules"
    if not gitmodules.is_file():
        return []

    tree = _git(worktree, "ls-tree", "-rz", "HEAD")
    commits: dict[str, str] = {}
    for record in tree.stdout.split("\0"):
        metadata, separator, path = record.partition("\t")
        if not separator:
            continue
        fields = metadata.split()
        if len(fields) == _LS_TREE_FIELD_COUNT and fields[0] == _SUBMODULE_MODE:
            commits[path] = fields[2]

    configured = _git(
        worktree,
        "config",
        "--file",
        str(gitmodules),
        "--get-regexp",
        r"^submodule\..*\.path$",
        check=False,
    )
    entries: list[_Submodule] = []
    for line in configured.stdout.splitlines():
        key, separator, path = line.partition(" ")
        match = _SUBMODULE_PATH_KEY.fullmatch(key)
        if not separator or match is None or path not in commits:
            continue
        entries.append(_Submodule(name=match.group("name"), path=path, commit=commits[path]))
    return entries


def _git_dir(repo: Path) -> Path | None:
    result = _git(repo, "rev-parse", "--absolute-git-dir", check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def _module_git_dir(superproject: Path, name: str) -> Path | None:
    result = _git(
        superproject,
        "rev-parse",
        "--path-format=absolute",
        "--git-path",
        f"modules/{name}",
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    path = Path(result.stdout.strip())
    return path if _git_dir(path) is not None else None


def _pool_superprojects(main_repo: Path, worktree: Path) -> list[Path]:
    """Return canonical then existing slot checkouts that can seed submodules."""
    sources = [main_repo]
    if not worktree.parent.is_dir():
        return sources
    destination = worktree.resolve()
    for candidate in sorted(worktree.parent.iterdir()):
        if not candidate.is_dir() or candidate.resolve() == destination:
            continue
        if _git_dir(candidate) is not None:
            sources.append(candidate)
    return sources


def _candidate_repositories(
    superprojects: list[Path],
    submodule: _Submodule,
) -> list[Path]:
    """Find checkout and stored-gitdir candidates at the corresponding path."""
    candidates: list[Path] = []
    seen_git_dirs: set[Path] = set()
    for superproject in superprojects:
        stored = _module_git_dir(superproject, submodule.name)
        for candidate in (superproject / submodule.path, stored):
            if candidate is None:
                continue
            git_dir = _git_dir(candidate)
            if git_dir is None or git_dir in seen_git_dirs:
                continue
            seen_git_dirs.add(git_dir)
            candidates.append(candidate)
    return candidates


def _url_identity(repo: Path, url: str) -> tuple[str, str]:
    """Normalize URL rewrites and local paths without contacting the remote."""
    expanded = _git(repo, "ls-remote", "--get-url", url, check=False)
    value = expanded.stdout.strip() if expanded.returncode == 0 else url
    if value.startswith("file://"):
        parsed = urlsplit(value)
        if parsed.netloc in {"", "localhost"}:
            return "file", str(Path(unquote(parsed.path)).resolve())
    if "://" not in value and _SCP_LIKE_URL.fullmatch(value) is None:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = repo / path
        return "file", str(path.resolve())
    return "remote", value.rstrip("/")


def _configured_url(superproject: Path, submodule: _Submodule) -> str:
    """Register and return Git's resolved URL for one submodule."""
    _git(superproject, "submodule", "sync", "--", submodule.path)
    _git(superproject, "submodule", "init", "--", submodule.path)
    result = _git(
        superproject,
        "config",
        "--get",
        f"submodule.{submodule.name}.url",
        check=False,
    )
    url = result.stdout.strip()
    if result.returncode != 0 or not url:
        raise CommandError(
            f"submodule '{submodule.path}' has no configured URL in {superproject / '.gitmodules'}"
        )
    return url


def _compatible_sources(
    superprojects: list[Path],
    submodule: _Submodule,
    configured_url: str,
    target_superproject: Path,
) -> list[Path]:
    wanted = _url_identity(target_superproject, configured_url)
    compatible: list[Path] = []
    for candidate in _candidate_repositories(superprojects, submodule):
        remote = _git(candidate, "remote", "get-url", "origin", check=False)
        if remote.returncode != 0 or not remote.stdout.strip():
            continue
        if _url_identity(candidate, remote.stdout.strip()) == wanted:
            compatible.append(candidate)
    return compatible


def _has_commit(repo: Path, commit: str) -> bool:
    result = run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"],
        env={"GIT_NO_LAZY_FETCH": "1"},
        check=False,
    )
    return result.returncode == 0


def _prepare_submodule(destination: Path, configured_url: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_dir() or destination.is_symlink():
            raise CommandError(f"refusing to replace non-directory submodule path {destination}")
        try:
            destination.rmdir()
        except OSError as error:
            message = f"refusing to replace non-empty submodule path {destination}"
            raise CommandError(message) from error
    run(["git", "init", "--quiet", str(destination)])
    _git(destination, "remote", "add", "origin", configured_url)


def _fetch_local_commit(destination: Path, source: Path, commit: str) -> bool:
    """Copy one committed object graph locally, without refs or worktree state."""
    result = run(
        [
            "git",
            "-C",
            str(destination),
            "-c",
            "protocol.file.allow=always",
            "fetch",
            "--quiet",
            "--no-tags",
            str(source),
            commit,
        ],
        env={"GIT_NO_LAZY_FETCH": "1"},
        check=False,
    )
    return result.returncode == 0


def _fetch_remote_commit(destination: Path, commit: str) -> None:
    exact = _git(
        destination,
        "fetch",
        "--quiet",
        "--no-tags",
        "origin",
        commit,
        check=False,
    )
    if exact.returncode != 0:
        _git(destination, "fetch", "--quiet", "--no-tags", "origin")


def _initialize_submodule(
    superproject: Path,
    submodule: _Submodule,
    configured_url: str,
    sources: list[Path],
) -> Path:
    """Initialize one standalone submodule from a local source or its origin."""
    destination = superproject / submodule.path
    try:
        _prepare_submodule(destination, configured_url)
        reused = any(
            _fetch_local_commit(destination, source, submodule.commit)
            for source in sources
            if _has_commit(source, submodule.commit)
        )
        if not reused:
            _fetch_remote_commit(destination, submodule.commit)
        _git(destination, "checkout", "--quiet", "--detach", submodule.commit)
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return destination


async def _init_submodules_recursive(
    superproject: Path,
    source_superprojects: list[Path],
    semaphore: asyncio.Semaphore,
) -> None:
    """Seed every submodule of ``superproject``, fanning out across the tree.

    Registration (``git submodule sync``/``init``) mutates the superproject's
    ``.git/config``, which Git guards with a ``config.lock`` — running it for
    sibling submodules concurrently would race. So each level first registers
    its submodules sequentially, then seeds their object stores and working
    trees in parallel. Nested submodules recurse under the *same* shared
    semaphore, so in-flight git children stay bounded by ``[concurrency].limit``
    across the whole tree, not per level.
    """
    prepared: list[tuple[_Submodule, str, list[Path]]] = []
    for submodule in _submodule_entries(superproject):
        configured_url = _configured_url(superproject, submodule)
        compatible = _compatible_sources(
            source_superprojects,
            submodule,
            configured_url,
            superproject,
        )
        prepared.append((submodule, configured_url, compatible))
    if not prepared:
        return

    async def _seed(submodule: _Submodule, configured_url: str, compatible: list[Path]) -> None:
        # `_initialize_submodule` is a blocking pipeline of git subprocesses on
        # a private destination repo; a thread keeps the event loop free while
        # it fetches and checks out. The semaphore caps concurrent children.
        async with semaphore:
            destination = await asyncio.to_thread(
                _initialize_submodule,
                superproject,
                submodule,
                configured_url,
                compatible,
            )
        await _init_submodules_recursive(destination, compatible, semaphore)

    async with asyncio.TaskGroup() as tg:
        for submodule, configured_url, compatible in prepared:
            tg.create_task(_seed(submodule, configured_url, compatible))


def init_submodules(main_repo: Path, worktree: Path) -> None:
    """Initialize exact submodule commits, preferring compatible local repositories.

    The superproject itself is a linked worktree and already shares its object
    store with ``main_repo``. Git does not propagate that sharing to separate
    submodule repositories, so seed each submodule with an exact-SHA fetch from
    the canonical checkout/object store or an existing pool slot. A local fetch
    transfers committed objects only; it cannot copy a source index, worktree,
    or untracked files, and leaves no alternate dependency on a deletable slot.

    Submodules are seeded in parallel (bounded by ``[concurrency].limit``);
    a repo that vendors several large source trees (e.g. hadron's per-version
    Postgres subrepos) otherwise pays for each fetch and checkout end to end.
    """

    async def _run() -> None:
        semaphore = asyncio.Semaphore(config.concurrency().limit)
        await _init_submodules_recursive(
            worktree,
            _pool_superprojects(main_repo, worktree),
            semaphore,
        )

    try:
        asyncio.run(_run())
    except BaseExceptionGroup as group:
        # `TaskGroup` reports child failures as an `ExceptionGroup`, but callers
        # (and `pool.add`'s cleanup) expect the original `CommandError`. The
        # group cancels siblings on the first failure, so surfacing the first
        # leaf matches the old sequential behavior of stopping at that error.
        raise _first_leaf_exception(group) from None
    # Use Git's recursive machinery as a final invariant check and URL sync.
    # Every configured submodule is already initialized at its pinned commit,
    # so this does not contact a remote after successful local reuse.
    update_submodules(worktree)


def _first_leaf_exception(error: BaseException) -> BaseException:
    """Descend nested `ExceptionGroup`s to the first underlying exception."""
    while isinstance(error, BaseExceptionGroup):
        error = error.exceptions[0]
    return error


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
