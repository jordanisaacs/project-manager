from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.slot import Slot
from project_manager.project.errors import ProjectError


def release(paths: Paths, project: str) -> list[Slot]:
    """Drop .owner for each slot currently owned by this project.

    Forward symlinks are left in place.
    Returns the list of slots that were released.
    """
    project_dir = paths.project(project)
    if not project_dir.is_dir():
        raise ProjectError(f"project '{project}' does not exist")

    released: list[Slot] = []
    for entry in sorted(project_dir.iterdir()):
        if not entry.is_symlink():
            continue
        target = entry.readlink()
        if not target.is_dir():
            continue
        s = Slot(repo=entry.name, uuid=target.name, path=target)
        owner = s.owner_target()
        if owner is not None and owner == entry:
            slot_mod.release(s)
            released.append(s)
    return released
