import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod


class Kind(StrEnum):
    ACTIVE = "active"      # healthy: forward ↔ slot both link back
    DETACHED = "detached"  # forward → slot, no .owner (post-release)
    STALE = "stale"        # forward → slot, .owner → different forward
    BROKEN = "broken"      # forward target does not exist
    ORPHAN = "orphan"      # slot has .owner but forward missing/wrong


@dataclass(frozen=True)
class Finding:
    kind: Kind
    slot_path: Path | None
    forward_path: Path | None
    detail: str


def _slot_findings(paths: Paths) -> Iterator[Finding]:
    if not paths.worktrees.is_dir():
        return
    for repo_dir in sorted(paths.worktrees.iterdir()):
        if not repo_dir.is_dir():
            continue
        for s in slot_mod.list_slots(paths, repo_dir.name):
            owner = s.owner_target()
            if owner is None:
                continue
            if not owner.is_symlink():
                yield Finding(
                    kind=Kind.ORPHAN,
                    slot_path=s.path,
                    forward_path=owner,
                    detail=f".owner → {owner} does not exist",
                )
                continue
            forward_target = owner.readlink()
            if forward_target != s.path:
                yield Finding(
                    kind=Kind.ORPHAN,
                    slot_path=s.path,
                    forward_path=owner,
                    detail=(
                        f".owner → {owner} but that forward points at {forward_target}, "
                        f"not back at the slot"
                    ),
                )


def _classify_forward(entry: Path, target: Path) -> Finding:
    if not target.is_dir():
        return Finding(
            kind=Kind.BROKEN,
            slot_path=target,
            forward_path=entry,
            detail=f"forward → {target} does not exist",
        )
    s = slot_mod.Slot(repo=entry.name, uuid=target.name, path=target)
    owner = s.owner_target()
    if owner is None:
        return Finding(
            kind=Kind.DETACHED,
            slot_path=s.path,
            forward_path=entry,
            detail="slot has no .owner (post-release)",
        )
    if owner == entry:
        return Finding(
            kind=Kind.ACTIVE,
            slot_path=s.path,
            forward_path=entry,
            detail="healthy",
        )
    return Finding(
        kind=Kind.STALE,
        slot_path=s.path,
        forward_path=entry,
        detail=f"slot .owner points at {owner}",
    )


def _forward_findings(paths: Paths) -> Iterator[Finding]:
    if not paths.projects.is_dir():
        return
    for project_dir in sorted(paths.projects.iterdir()):
        if not project_dir.is_dir():
            continue
        for entry in sorted(project_dir.iterdir()):
            if not entry.is_symlink():
                continue
            yield _classify_forward(entry, entry.readlink())


def check(paths: Paths) -> list[Finding]:
    return list(_slot_findings(paths)) + list(_forward_findings(paths))


def fix(paths: Paths, findings: list[Finding]) -> int:
    """Apply safe fixes. Returns count of applied fixes.

    Safe: orphan (remove stray .owner), broken (remove dangling forward symlink).
    Not touched: detached / stale (those are intentional post-release states).
    """
    del paths
    n = 0
    for f in findings:
        if f.kind == Kind.ORPHAN and f.slot_path is not None:
            owner_path = f.slot_path / slot_mod.OWNER_FILENAME
            with contextlib.suppress(FileNotFoundError):
                owner_path.unlink()
                n += 1
        elif f.kind == Kind.BROKEN and f.forward_path is not None:
            with contextlib.suppress(FileNotFoundError):
                f.forward_path.unlink()
                n += 1
    return n
