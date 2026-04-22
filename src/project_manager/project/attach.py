import contextlib
import sqlite3
from pathlib import Path

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.slot import PoolExhaustedError, Slot, SlotBusyError
from project_manager.project import db
from project_manager.project.errors import ProjectError


def _resolve_repos(
    project: str,
    repos: list[str] | None,
    all_rows: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    if repos is None:
        return all_rows
    known = dict(all_rows)
    missing = [r for r in repos if r not in known]
    if missing:
        raise ProjectError(
            f"project '{project}' has no such repo(s): {', '.join(missing)}"
        )
    return [(r, known[r]) for r in repos]


def _try_reclaim(paths: Paths, repo: str, slot_uuid: str, forward: Path) -> Slot | None:
    """Attempt to reclaim the remembered slot. Returns the claimed Slot, or None if unavailable."""
    slot_path = paths.slot(repo, slot_uuid)
    if not slot_path.is_dir():
        return None
    s = Slot(repo=repo, uuid=slot_uuid, path=slot_path)
    if not s.is_free():
        return None
    try:
        slot_mod.claim(s, forward)
    except SlotBusyError:
        return None
    return s


def _claim_fallback(paths: Paths, repo: str, forward: Path, retries: int = 3) -> Slot:
    for _ in range(retries):
        free = slot_mod.free_slots(paths, repo)
        if not free:
            raise PoolExhaustedError(
                f"no free slots for repo '{repo}' — run `pm pool add {repo}` or "
                f"detach/delete a project"
            )
        for s in free:
            try:
                slot_mod.claim(s, forward)
            except SlotBusyError:
                continue
            else:
                return s
    raise SlotBusyError(f"lost {retries} claim races for repo '{repo}'")


def _attach_one(
    paths: Paths,
    project: str,
    repo: str,
    remembered_uuid: str,
    conn: sqlite3.Connection,
) -> Slot | None:
    """Attach a single repo. Returns the newly-claimed slot, or None if already attached."""
    forward = paths.forward(project, repo)

    if forward.is_symlink():
        target = forward.readlink()
        s = Slot(repo=repo, uuid=target.name, path=target)
        owner = s.owner_target()
        if owner == forward:
            return None  # already active (idempotent)
        raise ProjectError(
            f"{forward} exists but is not owned by this project — run `pm check`"
        )

    s = _try_reclaim(paths, repo, remembered_uuid, forward)
    if s is None:
        s = _claim_fallback(paths, repo, forward)
        db.update_slot(conn, repo, s.uuid)
    try:
        forward.symlink_to(s.path)
    except BaseException:
        slot_mod.release(s)
        raise
    return s


def attach(paths: Paths, project: str, repos: list[str] | None) -> list[Slot]:
    """Attach the given repos (or all if `repos is None`).

    For each repo row in the db: try the remembered `slot_uuid`. If the slot is missing
    or not free, claim any free slot and UPDATE the row. Best-effort rollback across
    repos on failure.

    Returns the list of slots newly claimed.
    """
    db_path = paths.project_db(project)
    if not db_path.is_file():
        raise ProjectError(f"project '{project}' does not exist")

    attached: list[tuple[Slot, Path]] = []
    try:
        with db.transaction(db_path) as conn:
            to_attach = _resolve_repos(project, repos, db.list_repos(conn))
            for repo, remembered in to_attach:
                s = _attach_one(paths, project, repo, remembered, conn)
                if s is not None:
                    attached.append((s, paths.forward(project, repo)))
    except BaseException:
        for s, f in reversed(attached):
            with contextlib.suppress(FileNotFoundError):
                f.unlink()
            slot_mod.release(s)
        raise
    return [s for s, _ in attached]
