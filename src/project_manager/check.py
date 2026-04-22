import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.project import db


class Kind(StrEnum):
    ACTIVE = "active"                  # db row + healthy forward + owner
    DETACHED = "detached"              # db row, no forward (intentional post-detach)
    DRIFT = "drift"                    # forward points at a different slot than db says
    STALE = "stale"                    # forward exists, slot's .owner missing or wrong
    BROKEN = "broken"                  # forward points at a missing slot
    ORPHAN_FORWARD = "orphan-forward"  # forward exists, no db row
    ORPHAN_OWNER = "orphan-owner"      # slot .owner points at a forward not claimed by db


@dataclass(frozen=True)
class Finding:
    kind: Kind
    slot_path: Path | None
    forward_path: Path | None
    detail: str


def _project_dbs(paths: Paths) -> list[tuple[str, Path]]:
    """Return [(project_name, db_path), ...] for every project dir with a .pm.db."""
    if not paths.projects.is_dir():
        return []
    out = []
    for project_dir in sorted(paths.projects.iterdir()):
        if not project_dir.is_dir():
            continue
        db_path = paths.project_db(project_dir.name)
        if db_path.is_file():
            out.append((project_dir.name, db_path))
    return out


def _row_findings(
    paths: Paths, project: str, repo: str, slot_uuid: str
) -> Iterator[Finding]:
    forward = paths.forward(project, repo)
    if not forward.is_symlink():
        return  # detached, healthy

    target = forward.readlink()
    if not target.is_dir():
        yield Finding(
            kind=Kind.BROKEN,
            slot_path=target,
            forward_path=forward,
            detail=f"forward → {target} does not exist",
        )
        return

    s = slot_mod.Slot(repo=repo, uuid=target.name, path=target)
    owner = s.owner_target()
    if owner is None or owner != forward:
        yield Finding(
            kind=Kind.STALE,
            slot_path=target,
            forward_path=forward,
            detail=(
                "slot has no .owner"
                if owner is None
                else f"slot .owner points at {owner}"
            ),
        )
        return

    if target.name != slot_uuid:
        yield Finding(
            kind=Kind.DRIFT,
            slot_path=target,
            forward_path=forward,
            detail=f"db says slot_uuid={slot_uuid}, forward points at {target.name}",
        )
        return

    yield Finding(
        kind=Kind.ACTIVE,
        slot_path=target,
        forward_path=forward,
        detail="healthy",
    )


def _orphan_forwards(paths: Paths, project: str, db_rows: set[str]) -> Iterator[Finding]:
    project_dir = paths.project(project)
    for entry in sorted(project_dir.iterdir()):
        if not entry.is_symlink():
            continue
        if entry.name in db_rows:
            continue
        yield Finding(
            kind=Kind.ORPHAN_FORWARD,
            slot_path=None,
            forward_path=entry,
            detail="forward symlink with no matching db row",
        )


def _orphan_owners(paths: Paths, active_pairs: set[tuple[Path, Path]]) -> Iterator[Finding]:
    """Emit ORPHAN_OWNER for any slot `.owner` that is not backing an active attachment.

    An `(slot, forward)` pair is active iff all of:
      - the forward symlink exists and resolves to the slot
      - the forward's project has a `.pm.db` with a matching row
      - that row was emitted as ACTIVE by `_row_findings` (hence in `active_pairs`)
    """
    if not paths.worktrees.is_dir():
        return
    for repo_dir in sorted(paths.worktrees.iterdir()):
        if not repo_dir.is_dir():
            continue
        for s in slot_mod.list_slots(paths, repo_dir.name):
            owner = s.owner_target()
            if owner is None:
                continue
            if (s.path, owner) in active_pairs:
                continue
            yield Finding(
                kind=Kind.ORPHAN_OWNER,
                slot_path=s.path,
                forward_path=owner,
                detail=".owner does not back any active db-tracked attachment",
            )


def check(paths: Paths) -> list[Finding]:
    findings: list[Finding] = []
    active_pairs: set[tuple[Path, Path]] = set()  # (slot_path, forward_path)

    for project, db_path in _project_dbs(paths):
        with db.readonly(db_path) as conn:
            rows = db.list_repos(conn)
        db_rows = {name for name, _ in rows}
        for repo, slot_uuid in rows:
            forward = paths.forward(project, repo)
            if not forward.is_symlink():
                findings.append(
                    Finding(
                        kind=Kind.DETACHED,
                        slot_path=paths.slot(repo, slot_uuid),
                        forward_path=forward,
                        detail=f"row for {repo} has no forward symlink",
                    )
                )
                continue
            row_findings = list(_row_findings(paths, project, repo, slot_uuid))
            findings.extend(row_findings)
            for rf in row_findings:
                # ACTIVE and DRIFT both indicate the `.owner` is legitimately backing
                # a project-tracked forward; only the db's memory may differ.
                if rf.kind in (Kind.ACTIVE, Kind.DRIFT) and rf.slot_path is not None:
                    active_pairs.add((rf.slot_path, forward))
        findings.extend(_orphan_forwards(paths, project, db_rows))

    findings.extend(_orphan_owners(paths, active_pairs))

    return findings


def fix(paths: Paths, findings: list[Finding]) -> int:
    """Apply safe fixes. Returns count of applied fixes.

    Fixed: BROKEN (unlink forward), ORPHAN_FORWARD (unlink forward),
           ORPHAN_OWNER (remove .owner), STALE (unlink forward).
    Left alone: DETACHED (intentional), ACTIVE (healthy), DRIFT (info).
    """
    del paths
    n = 0
    for f in findings:
        if f.kind in (Kind.BROKEN, Kind.ORPHAN_FORWARD, Kind.STALE) and f.forward_path is not None:
            with contextlib.suppress(FileNotFoundError):
                f.forward_path.unlink()
                n += 1
        elif f.kind == Kind.ORPHAN_OWNER and f.slot_path is not None:
            owner_path = f.slot_path / slot_mod.OWNER_FILENAME
            with contextlib.suppress(FileNotFoundError):
                owner_path.unlink()
                n += 1
    return n
