from pathlib import Path

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.slot import Slot, SlotBusyError
from project_manager.project.errors import ProjectError


def _attach_one(entry: Path) -> Slot | None:
    """Attach a single forward symlink, returning the newly-claimed slot or None if no-op.

    Raises ProjectError on stale / broken / race.
    """
    target = entry.readlink()
    if not target.is_dir():
        raise ProjectError(
            f"forward {entry} points at missing slot {target} — run `pm check --fix`"
        )
    s = Slot(repo=entry.name, uuid=target.name, path=target)
    owner = s.owner_target()
    if owner == entry:
        return None
    if owner is not None:
        raise ProjectError(
            f"slot {s.path} is claimed by {owner} — resolve that project first"
        )
    try:
        slot_mod.claim(s, entry)
    except SlotBusyError as e:
        raise ProjectError(f"lost race to claim {s.path} — another attach won") from e
    return s


def attach(paths: Paths, project: str) -> list[Slot]:
    """Re-claim slots pointed at by this project's forward symlinks.

    For each forward symlink `~/.projects/<project>/<repo>` → `<slot>`:
      - if `<slot>/.owner` is absent → atomically claim it back
      - if `<slot>/.owner` already points at this forward → no-op (idempotent)
      - if `<slot>/.owner` points elsewhere → error (stale; resolve manually)
      - if `<slot>` target is missing → error (broken)

    Best-effort rollback on partial failure across forwards.
    Returns the list of slots newly claimed.
    """
    project_dir = paths.project(project)
    if not project_dir.is_dir():
        raise ProjectError(f"project '{project}' does not exist")

    forwards = [e for e in sorted(project_dir.iterdir()) if e.is_symlink()]
    attached: list[Slot] = []
    try:
        attached.extend(s for e in forwards if (s := _attach_one(e)) is not None)
    except BaseException:
        for s in reversed(attached):
            slot_mod.release(s)
        raise
    return attached
