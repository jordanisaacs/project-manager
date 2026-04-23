import contextlib

from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.pool.db import Owner, OwnerKind, PoolDB
from project_manager.pool.slot import Slot
from project_manager.project import branch as branch_mod
from project_manager.project import db


def detach(paths: Paths, project: str, repos: list[str] | None) -> list[Slot]:
    """Unlink forward symlinks and release pool-db rows for the given repos (or all).

    For each repo with a live forward link: hard-block if the slot has tracked
    uncommitted changes, save the current branch (or NULL) to the db, park the
    slot to a detached HEAD on the repo's default branch, then release+unlink.

    `slot_uuid` rows are untouched — re-attach remembers the slot. A dirty slot
    aborts immediately; any previously-processed repos in the same call remain
    fully detached (idempotent on retry).
    """
    db_path = paths.project_db(project)
    if not db_path.is_file():
        raise ProjectError(f"project '{project}' does not exist")

    pooldb = PoolDB(paths.pool_db())
    owner = Owner(OwnerKind.PROJECT, project)
    released: list[Slot] = []

    with db.transaction(db_path) as conn:
        all_rows = db.list_repos(conn)
        if repos is None:
            to_detach = [name for name, _ in all_rows]
        else:
            known = {name for name, _ in all_rows}
            missing = [r for r in repos if r not in known]
            if missing:
                raise ProjectError(
                    f"project '{project}' has no such repo(s): {', '.join(missing)}"
                )
            to_detach = list(repos)

        for repo in to_detach:
            forward = paths.forward(project, repo)
            if not forward.is_symlink():
                continue
            target = forward.readlink()
            uuid = target.name
            slot_path = paths.slot(repo, uuid)

            branch_mod.ensure_clean(slot_path, repo)
            current = branch_mod.read_current_branch(slot_path)
            db.set_branch(conn, repo, current)
            branch_mod.park_to_default(slot_path, paths.repo(repo))

            if pooldb.get_owner(repo, uuid) == owner:
                pooldb.release(repo, uuid)
                released.append(Slot(repo=repo, uuid=uuid, path=target))
            with contextlib.suppress(FileNotFoundError):
                forward.unlink()
    return released
