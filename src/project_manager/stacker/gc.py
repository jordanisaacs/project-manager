"""Garbage-collect stacker-ops pool slots that no longer back an active op."""
from __future__ import annotations

from pathlib import Path

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OwnerKind, PoolDB

from . import git
from .db import StackerDB
from .models import OperationState


def gc_ops(paths: Paths) -> list[slot_mod.Slot]:
    """Release stacker-ops slots whose repo has no active operation row.

    Safe to run at any time: a slot is only released if it is (a) recorded in
    the pool db as stacker-owned and (b) has no matching `operations` row in
    the stacker db pointing at its current branch.
    """
    if not paths.worktrees.is_dir():
        return []
    pooldb = PoolDB(paths.pool_db())
    db = StackerDB(paths.stacker_db())
    ops_by_repo = _ops_by_repo(db.list_operations())
    released: list[slot_mod.Slot] = []
    for repo, uuid, owner in pooldb.list_owned():
        if owner.kind != OwnerKind.STACKER:
            continue
        slot_path = paths.slot(repo, uuid)
        if not slot_path.is_dir():
            pooldb.release(repo, uuid)
            released.append(slot_mod.Slot(repo=repo, uuid=uuid, path=slot_path))
            continue
        branch = _slot_branch(slot_path)
        if branch and branch in ops_by_repo.get(repo, set()):
            continue
        pooldb.release(repo, uuid)
        released.append(slot_mod.Slot(repo=repo, uuid=uuid, path=slot_path))
    return released


def _ops_by_repo(ops: list[OperationState]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for op in ops:
        out.setdefault(op.repo_name, set())
        if op.branch:
            out[op.repo_name].add(op.branch)
    return out


def _slot_branch(slot_path: Path) -> str | None:
    try:
        return git.current_branch(slot_path) or None
    except git.GitError:
        return None
