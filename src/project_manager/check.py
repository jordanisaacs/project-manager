import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import Owner, OwnerKind, PoolDB
from project_manager.project import db, discovery
from project_manager.render import Column
from project_manager.stacker import git as stacker_git
from project_manager.stacker import ops_slot


class Kind(StrEnum):
    ACTIVE = "active"  # db row + forward + pool row aligned
    DETACHED = "detached"  # db row, no forward, no pool row
    DRIFT = "drift"  # forward points at slot != db's remembered uuid
    STALE = "stale"  # forward exists; pool db row missing or wrong owner
    BROKEN = "broken"  # forward points at a missing slot dir
    ORPHAN_FORWARD = "orphan-forward"  # forward exists, no db row
    ORPHAN_OWNER = "orphan-owner"  # project-owned pool row with no backing forward
    OPS_OWNED = "ops-owned"  # stacker-owned slot mid-op (resumable state present)
    STALE_OPS = "stale-ops"  # stacker-owned slot with a clean worktree — leaked


@dataclass(frozen=True)
class Finding:
    kind: Kind
    wt: str | None  # worktree name (symlink name under projects/<project>/)
    repo: str | None  # pool repo key (may be None when unknowable, e.g. bare orphan forward)
    slot_path: Path | None
    forward_path: Path | None
    detail: str

    def __pm_json__(self) -> dict:
        return {
            "kind": self.kind.value,
            "wt": self.wt,
            "repo": self.repo,
            "slot_path": str(self.slot_path) if self.slot_path else None,
            "forward_path": str(self.forward_path) if self.forward_path else None,
            "detail": self.detail,
        }


_KIND_STYLE: dict[Kind, str] = {
    Kind.ACTIVE: "green",
    Kind.DETACHED: "dim",
    Kind.DRIFT: "yellow",
    Kind.STALE: "red",
    Kind.BROKEN: "red",
    Kind.ORPHAN_FORWARD: "red",
    Kind.ORPHAN_OWNER: "red",
    Kind.OPS_OWNED: "cyan",
    Kind.STALE_OPS: "red",
}


# `pm check` output is flat; CLI calls `render.emit_rows(findings, COLUMNS)`.
COLUMNS: list[Column] = [
    Column(
        "Kind",
        lambda r: r.kind.value,
        style=lambda r: _KIND_STYLE.get(r.kind, ""),
    ),
    Column("Forward", lambda r: str(r.forward_path or "-")),
    Column("Slot", lambda r: str(r.slot_path or "-"), style="dim"),
    Column("Detail", "detail"),
]


def _row_findings(
    paths: Paths,
    pooldb: PoolDB,
    project: str,
    row: tuple[str, str, str],
) -> Iterator[Finding]:
    wt, repo, slot_uuid = row
    forward = paths.forward(project, wt)
    if not forward.is_symlink():
        return  # detached — caller emits DETACHED

    target = forward.readlink()
    if not target.is_dir():
        yield Finding(
            kind=Kind.BROKEN,
            wt=wt,
            repo=repo,
            slot_path=target,
            forward_path=forward,
            detail=f"forward → {target} does not exist",
        )
        return

    uuid = target.name
    expected = Owner(OwnerKind.PROJECT, project)
    owner = pooldb.get_owner(repo, uuid)
    if owner != expected:
        yield Finding(
            kind=Kind.STALE,
            wt=wt,
            repo=repo,
            slot_path=target,
            forward_path=forward,
            detail=(
                "pool db has no row for this slot"
                if owner is None
                else f"pool db says owner is {owner.kind.value}:{owner.id}"
            ),
        )
        return

    if uuid != slot_uuid:
        yield Finding(
            kind=Kind.DRIFT,
            wt=wt,
            repo=repo,
            slot_path=target,
            forward_path=forward,
            detail=f"db says slot_uuid={slot_uuid}, forward points at {uuid}",
        )
        return

    yield Finding(
        kind=Kind.ACTIVE,
        wt=wt,
        repo=repo,
        slot_path=target,
        forward_path=forward,
        detail="healthy",
    )


def _orphan_forwards(
    paths: Paths,
    project: str,
    db_wts: set[str],
) -> Iterator[Finding]:
    project_dir = paths.project(project)
    for entry in sorted(project_dir.iterdir()):
        if not entry.is_symlink():
            continue
        if entry.name in db_wts:
            continue
        # If the symlink target is reachable, its parent dir name is the repo.
        target = entry.readlink()
        repo = target.parent.name if target.parent.is_dir() else None
        yield Finding(
            kind=Kind.ORPHAN_FORWARD,
            wt=entry.name,
            repo=repo,
            slot_path=None,
            forward_path=entry,
            detail="forward symlink with no matching db row",
        )


def _classify_pool_rows(
    paths: Paths,
    pooldb: PoolDB,
    legit_project_rows: set[tuple[str, str, str]],
    *,
    scope: str | None = None,
) -> Iterator[Finding]:
    """Emit OPS_OWNED / ORPHAN_OWNER for every row in the pool db.

    `legit_project_rows` is the set of (project, repo, uuid) triples that are
    backed by a live forward symlink (ACTIVE or DRIFT). Project-owned pool rows
    outside this set are ORPHAN_OWNER. STACKER-owned rows are always OPS_OWNED.

    If `scope` is given, only emit findings for project-owned rows whose
    owner_id == scope; STACKER rows are filtered out.
    """
    for repo, uuid, owner in pooldb.list_owned():
        slot_path = paths.slot(repo, uuid)
        if owner.kind == OwnerKind.STACKER:
            if scope is not None:
                continue
            # Split stacker-ops claims by whether the worktree still has
            # work to preserve. STALE_OPS means a previous push/sync
            # leaked the claim — `--fix` can safely release it. OPS_OWNED
            # means there's a paused cherry-pick or uncommitted change
            # that `pm stacker continue` will pick up; we must hold.
            stale = slot_path.is_dir() and not stacker_git.has_resumable_state(
                slot_path,
            )
            yield Finding(
                kind=Kind.STALE_OPS if stale else Kind.OPS_OWNED,
                wt=None,
                repo=repo,
                slot_path=slot_path,
                forward_path=None,
                detail=(
                    "stacker ops claim with a clean worktree — leaked"
                    if stale
                    else "slot held by a stacker ops operation"
                ),
            )
            continue
        if scope is not None and owner.id != scope:
            continue
        if (owner.id, repo, uuid) in legit_project_rows:
            continue
        yield Finding(
            kind=Kind.ORPHAN_OWNER,
            wt=None,
            repo=repo,
            slot_path=slot_path,
            forward_path=None,
            detail=(
                f"pool row owned by {owner.kind.value}:{owner.id} but not backed by a live forward"
            ),
        )


def _project_findings(
    paths: Paths,
    pooldb: PoolDB,
    project: str,
) -> tuple[list[Finding], set[tuple[str, str, str]]]:
    """Row findings + orphan_forwards for one project, plus legit (project, repo, uuid) triples."""
    db_path = discovery.require_project_db(paths, project)
    with db.readonly(db_path) as conn:
        rows = db.list_wts(conn)
    db_wts = {wt for wt, _, _ in rows}

    findings: list[Finding] = []
    legit: set[tuple[str, str, str]] = set()
    for wt, repo, slot_uuid in rows:
        forward = paths.forward(project, wt)
        if not forward.is_symlink():
            findings.append(
                Finding(
                    kind=Kind.DETACHED,
                    wt=wt,
                    repo=repo,
                    slot_path=paths.slot(repo, slot_uuid),
                    forward_path=forward,
                    detail=f"row for {wt} has no forward symlink",
                ),
            )
            continue
        row_findings = list(_row_findings(paths, pooldb, project, (wt, repo, slot_uuid)))
        findings.extend(row_findings)
        for rf in row_findings:
            # ACTIVE and DRIFT both indicate the pool row is legitimately backing
            # a project-tracked forward; only the db's memory may differ.
            if (
                rf.kind in (Kind.ACTIVE, Kind.DRIFT)
                and rf.slot_path is not None
                and rf.repo is not None
            ):
                legit.add((project, rf.repo, rf.slot_path.name))
    findings.extend(_orphan_forwards(paths, project, db_wts))
    return findings, legit


def check_project(
    paths: Paths,
    project: str,
    *,
    include_orphan_owners: bool = True,
) -> list[Finding]:
    """All findings for a single project.

    Row-level findings + orphan_forwards always. When `include_orphan_owners`
    is True, also yields ORPHAN_OWNER for pool-db rows owned by this project
    that aren't backing a live forward.
    """
    pooldb = PoolDB(paths.pool_db())
    findings, legit = _project_findings(paths, pooldb, project)
    if include_orphan_owners:
        findings.extend(_classify_pool_rows(paths, pooldb, legit, scope=project))
    return findings


def check(paths: Paths) -> list[Finding]:
    pooldb = PoolDB(paths.pool_db())
    findings: list[Finding] = []
    legit: set[tuple[str, str, str]] = set()

    for project, _ in discovery.list_project_dbs(paths):
        project_findings, project_legit = _project_findings(paths, pooldb, project)
        findings.extend(project_findings)
        legit |= project_legit

    findings.extend(_classify_pool_rows(paths, pooldb, legit))
    return findings


def fix(paths: Paths, findings: list[Finding]) -> int:
    """Apply safe fixes. Returns count of applied fixes.

    Fixed: BROKEN (unlink forward), ORPHAN_FORWARD (unlink forward),
           ORPHAN_OWNER (release pool row), STALE (unlink forward),
           STALE_OPS (detach HEAD + release pool row).
    Left alone: DETACHED (intentional), ACTIVE (healthy), DRIFT (info),
                OPS_OWNED (mid-op — `pm stacker continue` owns it).
    """
    pooldb = PoolDB(paths.pool_db())
    n = 0
    for f in findings:
        if f.kind in (Kind.BROKEN, Kind.ORPHAN_FORWARD, Kind.STALE) and f.forward_path is not None:
            with contextlib.suppress(FileNotFoundError):
                f.forward_path.unlink()
                n += 1
        elif f.kind == Kind.ORPHAN_OWNER and f.slot_path is not None:
            pooldb.release(f.slot_path.parent.name, f.slot_path.name)
            n += 1
        elif f.kind == Kind.STALE_OPS and f.slot_path is not None and f.repo is not None:
            # Re-check `has_resumable_state` here: the classification was
            # observed earlier in the run, and a concurrent stacker op
            # could have entered a paused state since. Cheap insurance
            # against detaching from a worktree that's now mid-conflict.
            if stacker_git.has_resumable_state(f.slot_path):
                continue
            ops_slot.release(
                pooldb,
                slot_mod.Slot(repo=f.repo, uuid=f.slot_path.name, path=f.slot_path),
            )
            n += 1
    return n
