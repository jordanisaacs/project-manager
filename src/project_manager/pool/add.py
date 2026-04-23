import subprocess
import uuid as uuid_mod

from project_manager.errors import CommandError
from project_manager.paths import Paths
from project_manager.pool import worktree as wt
from project_manager.pool.slot import Slot


def add(paths: Paths, repo: str) -> Slot:
    """Mint a new UUID slot under the pool for `repo`.

    1. git worktree add --detach <slot> <default-branch>
    2. init_submodules (--reference sharing)
    3. copy untracked CLAUDE.md / CLAUDE.local.md
    4. run init.sh if present
    """
    main_repo = paths.repo(repo)
    if not (main_repo / ".git").exists():
        raise CommandError(f"no git repo at {main_repo}")

    pool_dir = paths.pool(repo)
    pool_dir.mkdir(parents=True, exist_ok=True)

    u = str(uuid_mod.uuid4())
    slot_path = paths.slot(repo, u)

    branch = wt.default_branch(main_repo)
    wt.worktree_add_detached(main_repo, slot_path, branch)
    try:
        wt.init_submodules(main_repo, slot_path)
        wt.copy_claude_files(main_repo, slot_path)
        wt.run_init_script(main_repo, slot_path)
    except BaseException:
        subprocess.run(
            ["git", "-C", str(main_repo), "worktree", "remove", "--force", str(slot_path)],
            check=False,
        )
        raise

    return Slot(repo=repo, uuid=u, path=slot_path)
