import contextlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import Owner, OwnerKind, PoolDB
from project_manager.pool.slot import PoolExhaustedError, Slot, SlotBusyError
from project_manager.project import db
from project_manager.render import Column

_README_TEMPLATE = """\
# {project}

This directory is a **`pm`-managed project**, not a git repository.

It is a container of symlinks into a shared worktree pool managed by `pm`.
Each entry (besides `.pm.db` and this README) is a symlink to a worktree
checkout living under the pool root.

## Inspecting state

- `ls` / `ls -l` — see attached worktrees (symlinks) at a glance.
- `pm project status` — authoritative state from `.pm.db`.

Do not `git init` here, do not commit this directory, and do not move the
symlinks by hand — use `pm project wt attach|detach|delete`.
"""


@dataclass(frozen=True)
class CreatedWt:
    wt: str
    repo: str
    uuid: str
    path: Path


CREATED_COLUMNS: list[Column] = [
    Column("Worktree", "wt"),
    Column("Repo", "repo", style="blue"),
    Column("UUID", "uuid", style="dim"),
    Column("Path", "path"),
]


@dataclass(frozen=True)
class _CreateCtx:
    paths: Paths
    pooldb: PoolDB
    conn: sqlite3.Connection


def _claim_any_free(
    paths: Paths,
    pooldb: PoolDB,
    owner: Owner,
    repo: str,
    retries: int = 3,
) -> Slot:
    for _ in range(retries):
        free = slot_mod.free_slots(paths, pooldb, repo)
        if not free:
            raise PoolExhaustedError(
                f"no free slots for repo '{repo}' — run `pm pool add {repo}` or "
                f"detach/delete a project"
            )
        for s in free:
            try:
                pooldb.claim(repo, s.uuid, owner)
            except SlotBusyError:
                continue
            else:
                return s
    raise SlotBusyError(f"lost {retries} claim races for repo '{repo}'")


def _claim_one(
    ctx: _CreateCtx,
    project: str,
    wt: str,
    repo: str,
) -> Slot:
    if db.get_slot(ctx.conn, wt) is not None:
        raise ProjectError(f"project '{project}' already has worktree '{wt}'")
    forward = ctx.paths.forward(project, wt)
    if forward.is_symlink() or forward.exists():
        raise ProjectError(f"{forward} already exists")
    owner = Owner(OwnerKind.PROJECT, project)
    s = _claim_any_free(ctx.paths, ctx.pooldb, owner, repo)
    try:
        forward.symlink_to(s.path)
        db.add_wt(ctx.conn, wt, repo, s.uuid)
    except BaseException:
        ctx.pooldb.release(repo, s.uuid)
        raise
    return s


def create(
    paths: Paths,
    project: str,
    wts: list[tuple[str, str]],
) -> list[CreatedWt]:
    """Create a project (or add worktrees to an existing one).

    `wts` is a list of `(wt_name, repo)` pairs. For each: claim a pool slot
    for `repo`, symlink `projects/<project>/<wt_name>` to the slot, insert
    a db row keyed on `wt_name`.

    Best-effort rollback on any failure: db transaction rolls back via the
    context manager; filesystem side-effects (symlinks) and pool-db rows
    are unwound by the outer except block. When the project dir or db was
    created by this call and the call fails, both are torn down too.
    """
    _validate_wts(wts)

    project_dir = paths.project(project)
    db_path = paths.project_db(project)
    created_dir = not project_dir.exists()
    db_existed = db_path.exists()

    pooldb = PoolDB(paths.pool_db())
    claimed: list[CreatedWt] = []
    try:
        with db.transaction(db_path) as conn:
            ctx = _CreateCtx(paths=paths, pooldb=pooldb, conn=conn)
            for wt, repo in wts:
                s = _claim_one(ctx, project, wt, repo)
                claimed.append(CreatedWt(wt=wt, repo=s.repo, uuid=s.uuid, path=s.path))
    except BaseException:
        for c in reversed(claimed):
            forward = paths.forward(project, c.wt)
            with contextlib.suppress(FileNotFoundError):
                forward.unlink()
            pooldb.release(c.repo, c.uuid)
        if not db_existed:
            with contextlib.suppress(FileNotFoundError):
                db_path.unlink()
        if created_dir:
            with contextlib.suppress(OSError):
                project_dir.rmdir()
        raise

    readme = project_dir / "README.md"
    if not readme.exists():
        readme.write_text(_README_TEMPLATE.format(project=project), encoding="utf-8")

    return claimed


def _validate_wts(wts: list[tuple[str, str]]) -> None:
    seen: set[str] = set()
    for wt, repo in wts:
        if not wt:
            raise ProjectError("worktree name must be non-empty")
        if not repo:
            raise ProjectError(f"worktree '{wt}' has empty repo")
        if wt in seen:
            raise ProjectError(f"duplicate worktree name '{wt}' in spec")
        seen.add(wt)
