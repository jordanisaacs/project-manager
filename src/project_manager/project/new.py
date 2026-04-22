import contextlib
from pathlib import Path

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.slot import PoolExhaustedError, Slot, SlotBusyError
from project_manager.project.errors import ProjectError


def _claim_any_free(paths: Paths, repo: str, forward: Path, retries: int = 3) -> Slot:
    for _ in range(retries):
        free = slot_mod.free_slots(paths, repo)
        if not free:
            raise PoolExhaustedError(
                f"no free slots for repo '{repo}' — run `pm pool add {repo}` or release a project"
            )
        for s in free:
            try:
                slot_mod.claim(s, forward)
            except SlotBusyError:
                continue
            else:
                return s
    raise SlotBusyError(f"lost {retries} claim races for repo '{repo}'")


def _claim_one_repo(
    paths: Paths, project: str, repo: str
) -> tuple[Slot, Path]:
    forward = paths.forward(project, repo)
    if forward.is_symlink() or forward.exists():
        raise ProjectError(f"{forward} already exists")
    slot = _claim_any_free(paths, repo, forward)
    try:
        forward.symlink_to(slot.path)
    except BaseException:
        slot_mod.release(slot)
        raise
    return slot, forward


def new(paths: Paths, project: str, repos: list[str]) -> list[tuple[str, Slot]]:
    """Claim a slot for each repo and create forward symlinks.

    Best-effort rollback on any failure.
    Returns [(repo, slot), ...] on success.
    """
    if not repos:
        raise ProjectError("must specify at least one repo")

    project_dir = paths.project(project)
    created_project_dir = False
    if not project_dir.exists():
        project_dir.mkdir(parents=True)
        created_project_dir = True

    claimed: list[tuple[str, Slot, Path]] = []
    try:
        for repo in repos:
            slot, forward = _claim_one_repo(paths, project, repo)
            claimed.append((repo, slot, forward))
    except BaseException:
        for _repo, s, f in reversed(claimed):
            with contextlib.suppress(FileNotFoundError):
                f.unlink()
            slot_mod.release(s)
        if created_project_dir:
            with contextlib.suppress(OSError):
                project_dir.rmdir()
        raise
    return [(r, s) for r, s, _ in claimed]
