import contextlib
from dataclasses import dataclass
from pathlib import Path

from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.pool.db import Owner, OwnerKind, PoolDB
from project_manager.project import branch as branch_mod
from project_manager.project import db
from project_manager.render import Column


@dataclass(frozen=True)
class DetachedWt:
    wt: str
    repo: str
    uuid: str
    path: Path


DETACHED_COLUMNS: list[Column] = [
    Column("Worktree", "wt"),
    Column("Repo", "repo", style="blue"),
    Column("UUID", "uuid", style="dim"),
    Column("Path", "path"),
]


def detach(paths: Paths, project: str, wts: list[str] | None) -> list[DetachedWt]:
    """Unlink forward symlinks and release pool-db rows for the given worktrees (or all).

    For each worktree with a live forward link: hard-block if the slot has
    tracked uncommitted changes, save the current branch (or NULL) to the
    db, park the slot to a detached HEAD on the repo's default branch, then
    release+unlink.

    `slot_uuid` rows are untouched — re-attach remembers the slot. A dirty
    slot aborts immediately; any previously-processed worktrees in the same
    call remain fully detached (idempotent on retry).
    """
    db_path = paths.project_db(project)
    if not db_path.is_file():
        raise ProjectError(f"project '{project}' does not exist")

    pooldb = PoolDB(paths.pool_db())
    owner = Owner(OwnerKind.PROJECT, project)
    released: list[DetachedWt] = []

    with db.transaction(db_path) as conn:
        all_rows = db.list_wts(conn)
        if wts is None:
            to_detach = [(w, r) for w, r, _ in all_rows]
        else:
            known = {w: r for w, r, _ in all_rows}
            missing = [w for w in wts if w not in known]
            if missing:
                raise ProjectError(
                    f"project '{project}' has no such worktree(s): {', '.join(missing)}"
                )
            to_detach = [(w, known[w]) for w in wts]

        for wt, repo in to_detach:
            forward = paths.forward(project, wt)
            if not forward.is_symlink():
                continue
            target = forward.readlink()
            uuid = target.name
            slot_path = paths.slot(repo, uuid)

            branch_mod.ensure_clean(slot_path, wt)
            current = branch_mod.read_current_branch(slot_path)
            db.set_branch(conn, wt, current)
            branch_mod.park_to_default(slot_path, paths.repo(repo))

            if pooldb.get_owner(repo, uuid) == owner:
                pooldb.release(repo, uuid)
                released.append(
                    DetachedWt(wt=wt, repo=repo, uuid=uuid, path=target)
                )
            with contextlib.suppress(FileNotFoundError):
                forward.unlink()
    return released


@dataclass(frozen=True)
class DetachAction:
    wt: str
    repo: str
    kind: str  # "detach" | "noop"
    slot_uuid: str | None
    blocker: str | None


@dataclass(frozen=True)
class DetachPlan:
    project: str
    actions: list[DetachAction]

    @property
    def has_blocker(self) -> bool:
        return any(a.blocker is not None for a in self.actions)

    def __pm_json__(self) -> dict:
        return {
            "project": self.project,
            "has_blocker": self.has_blocker,
            "actions": [
                {
                    "wt": a.wt,
                    "repo": a.repo,
                    "kind": a.kind,
                    "slot_uuid": a.slot_uuid,
                    "blocker": a.blocker,
                }
                for a in self.actions
            ],
        }


def _detach_status(action: DetachAction) -> str:
    if action.blocker is not None:
        return "blocked"
    return action.kind  # "detach" | "noop"


def _detach_status_style(action: DetachAction) -> str:
    if action.blocker is not None:
        return "red"
    if action.kind == "noop":
        return "dim"
    return "yellow"


def _detach_note(action: DetachAction) -> str:
    if action.blocker is not None:
        return action.blocker
    if action.kind == "noop":
        return "already detached"
    return f"release slot {action.slot_uuid}" if action.slot_uuid else ""


DETACH_ACTION_COLUMNS: list[Column] = [
    Column("Status", _detach_status, style=_detach_status_style),
    Column("Worktree", "wt"),
    Column("Note", _detach_note),
]


def plan_detach(
    paths: Paths, project: str, wts: list[str] | None,
) -> DetachPlan:
    """Describe what `detach` would do without mutating state.

    For each target worktree: reports either a detach action (with the slot
    uuid being released) or a noop (already detached). Cleanliness issues
    are surfaced as a blocker string instead of raising.
    """
    db_path = paths.project_db(project)
    if not db_path.is_file():
        raise ProjectError(f"project '{project}' does not exist")

    with db.readonly(db_path) as conn:
        all_rows = db.list_wts(conn)
    if wts is None:
        to_plan = [(w, r) for w, r, _ in all_rows]
    else:
        known = {w: r for w, r, _ in all_rows}
        missing = [w for w in wts if w not in known]
        if missing:
            raise ProjectError(
                f"project '{project}' has no such worktree(s): {', '.join(missing)}"
            )
        to_plan = [(w, known[w]) for w in wts]

    actions: list[DetachAction] = []
    for wt, repo in to_plan:
        forward = paths.forward(project, wt)
        if not forward.is_symlink():
            actions.append(
                DetachAction(
                    wt=wt, repo=repo, kind="noop", slot_uuid=None, blocker=None,
                )
            )
            continue
        target = forward.readlink()
        uuid = target.name
        slot_path = paths.slot(repo, uuid)
        if not slot_path.is_dir():
            actions.append(
                DetachAction(
                    wt=wt,
                    repo=repo,
                    kind="detach",
                    slot_uuid=uuid,
                    blocker="slot directory missing",
                )
            )
            continue
        blocker = branch_mod.cleanliness_blocker(slot_path)
        actions.append(
            DetachAction(
                wt=wt, repo=repo, kind="detach", slot_uuid=uuid, blocker=blocker,
            )
        )
    return DetachPlan(project=project, actions=actions)
