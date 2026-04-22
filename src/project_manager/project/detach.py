import contextlib

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.slot import Slot
from project_manager.project import db
from project_manager.project.errors import ProjectError


def detach(paths: Paths, project: str, repos: list[str] | None) -> list[Slot]:
    """Unlink forward symlinks and release `.owner` for the given repos (or all).

    Db rows are untouched — re-attach remembers the slot.
    Returns the list of slots that were released.
    """
    db_path = paths.project_db(project)
    if not db_path.is_file():
        raise ProjectError(f"project '{project}' does not exist")

    with db.readonly(db_path) as conn:
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

    released: list[Slot] = []
    for repo in to_detach:
        forward = paths.forward(project, repo)
        if not forward.is_symlink():
            continue  # already detached
        target = forward.readlink()
        s = Slot(repo=repo, uuid=target.name, path=target)
        owner = s.owner_target()
        if owner == forward:
            slot_mod.release(s)
            released.append(s)
        with contextlib.suppress(FileNotFoundError):
            forward.unlink()
    return released
