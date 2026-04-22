import contextlib
from dataclasses import dataclass
from pathlib import Path

from project_manager.paths import Paths

OWNER_FILENAME = ".owner"


class SlotBusyError(Exception):
    """Another claimer won the race to create .owner."""


class PoolExhaustedError(Exception):
    """No free slots under the pool."""


@dataclass(frozen=True)
class Slot:
    repo: str
    uuid: str
    path: Path

    @property
    def owner_path(self) -> Path:
        return self.path / OWNER_FILENAME

    def owner_target(self) -> Path | None:
        if not self.owner_path.is_symlink():
            return None
        return self.owner_path.readlink()

    def is_free(self) -> bool:
        return not self.owner_path.is_symlink()


def list_slots(paths: Paths, repo: str) -> list[Slot]:
    pool_dir = paths.pool(repo)
    if not pool_dir.is_dir():
        return []
    slots: list[Slot] = []
    for entry in sorted(pool_dir.iterdir()):
        if not entry.is_dir():
            continue
        slots.append(Slot(repo=repo, uuid=entry.name, path=entry))
    return slots


def free_slots(paths: Paths, repo: str) -> list[Slot]:
    return [s for s in list_slots(paths, repo) if s.is_free()]


def claim(slot: Slot, forward_link: Path) -> None:
    """Atomically create slot/.owner -> forward_link.

    Raises SlotBusyError if another claimer already created .owner.
    """
    try:
        slot.owner_path.symlink_to(forward_link)
    except FileExistsError as e:
        raise SlotBusyError(f"slot {slot.repo}/{slot.uuid} is already claimed") from e


def release(slot: Slot) -> None:
    """Remove slot/.owner. Idempotent."""
    with contextlib.suppress(FileNotFoundError):
        slot.owner_path.unlink()
