from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from project_manager.paths import Paths

if TYPE_CHECKING:
    from project_manager.pool.db import PoolDB


class SlotBusyError(Exception):
    """Another claimer won the race to record ownership."""


class PoolExhaustedError(Exception):
    """No free slots under the pool."""


@dataclass(frozen=True)
class Slot:
    repo: str
    uuid: str
    path: Path


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


def free_slots(paths: Paths, pooldb: PoolDB, repo: str) -> list[Slot]:
    return [s for s in list_slots(paths, repo) if pooldb.is_free(repo, s.uuid)]
