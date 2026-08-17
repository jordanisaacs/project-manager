from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from project_manager.errors import CommandError
from project_manager.paths import Paths
from project_manager.pool.db import Owner, OwnerKind, PoolDB
from project_manager.pool.slot import SlotBusyError
from project_manager.project import branch as branch_mod
from project_manager.render import Column
from project_manager.stacker import git

_DELETE_OWNER = Owner(OwnerKind.STACKER, "pool-delete")


@dataclass(frozen=True)
class DeletePlan:
    repo: str
    uuid: str
    path: Path
    branch: str | None
    blocker: str | None

    @property
    def has_blocker(self) -> bool:
        return self.blocker is not None

    def __pm_json__(self) -> dict[str, object]:
        return {
            "repo": self.repo,
            "uuid": self.uuid,
            "path": str(self.path),
            "branch": self.branch,
            "action": "delete",
            "safe": not self.has_blocker,
            "blocker": self.blocker,
        }


@dataclass(frozen=True)
class DeletedSlot:
    repo: str
    uuid: str
    path: Path
    status: str = "deleted"


def _plan_status(plan: DeletePlan) -> str:
    return "blocked" if plan.has_blocker else "delete"


def _plan_style(plan: DeletePlan) -> str:
    return "red" if plan.has_blocker else "yellow"


PLAN_COLUMNS: list[Column[DeletePlan]] = [
    Column("Status", _plan_status, style=_plan_style),
    Column("Repo", "repo", style="blue"),
    Column("UUID", "uuid", style="dim"),
    Column("Branch", "branch", empty="(detached)"),
    Column("Path", "path"),
    Column("Blocker", "blocker", empty="-"),
]

DELETED_COLUMNS: list[Column[DeletedSlot]] = [
    Column("Status", "status", style="green"),
    Column("Repo", "repo", style="blue"),
    Column("UUID", "uuid", style="dim"),
    Column("Path", "path"),
]


def _require_path_component(value: str, label: str) -> None:
    candidate = Path(value)
    if not value or candidate.is_absolute() or candidate.name != value or value in {".", ".."}:
        raise CommandError(f"{label} must be one exact path component")


def _owner_blocker(owner: Owner) -> str:
    if owner.kind == OwnerKind.PROJECT:
        return f"slot is claimed by project '{owner.id}'"
    if owner == _DELETE_OWNER:
        return "slot deletion is already in progress"
    return f"slot is claimed by stacker operation '{owner.id}'"


def _worktree_registration(main_repo: Path, slot_path: Path) -> tuple[bool, bool]:
    """Return whether `slot_path` is registered and whether that entry is locked."""
    out = git.git(main_repo, "worktree", "list", "--porcelain").stdout
    wanted = slot_path.resolve()
    for block in out.split("\n\n"):
        lines = block.splitlines()
        path_line = next((line for line in lines if line.startswith("worktree ")), None)
        if path_line is None:
            continue
        registered_path = Path(path_line.removeprefix("worktree "))
        if registered_path.resolve() != wanted:
            continue
        locked = any(line == "locked" or line.startswith("locked ") for line in lines)
        return True, locked
    return False, False


def _head_is_reachable(slot_path: Path) -> bool:
    result = git.git(
        slot_path,
        "for-each-ref",
        "--format=%(refname)",
        "--contains=HEAD",
        "refs/heads",
        "refs/remotes",
        "refs/tags",
        check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _attached_project_link(paths: Paths, slot_path: Path) -> Path | None:
    """Find a live project symlink to the slot, including pool-db drift."""
    if not paths.projects.is_dir():
        return None
    wanted = slot_path.resolve()
    for project_dir in paths.projects.iterdir():
        if not project_dir.is_dir():
            continue
        for entry in project_dir.iterdir():
            if entry.is_symlink() and entry.resolve() == wanted:
                return entry
    return None


def _worktree_state(
    paths: Paths,
    main_repo: Path,
    slot_path: Path,
) -> tuple[str | None, str | None]:
    """Return `(branch, blocker)` for an otherwise-unclaimed slot."""
    project_link = _attached_project_link(paths, slot_path)
    if project_link is not None:
        return None, f"slot is still attached at {project_link}; run `pm check --fix`"
    registered, locked = _worktree_registration(main_repo, slot_path)
    if not registered:
        return None, "slot is not a registered Git worktree"
    if locked:
        return None, "Git worktree is locked"
    branch = branch_mod.read_current_branch(slot_path)
    blocker = branch_mod.cleanliness_blocker(slot_path)
    if blocker is not None:
        return branch, blocker
    if not _head_is_reachable(slot_path):
        return branch, "HEAD is not reachable from any local, remote-tracking, or tag ref"
    return branch, None


def _inspect(
    paths: Paths,
    repo: str,
    uuid: str,
    *,
    ignore_owner: Owner | None = None,
) -> DeletePlan:
    _require_path_component(repo, "repo")
    _require_path_component(uuid, "UUID")
    main_repo = paths.repo(repo)
    slot_path = paths.slot(repo, uuid)
    if not (main_repo / ".git").exists():
        raise CommandError(f"no git repo at {main_repo}")
    if slot_path.is_symlink():
        raise CommandError(f"refusing symlinked pool slot at {slot_path}")
    if not slot_path.is_dir():
        raise CommandError(f"no pool slot at {slot_path}")

    owner = PoolDB(paths.pool_db()).get_owner(repo, uuid)
    blocker = _owner_blocker(owner) if owner is not None and owner != ignore_owner else None
    branch: str | None = None
    if blocker is None:
        branch, blocker = _worktree_state(paths, main_repo, slot_path)
    return DeletePlan(repo=repo, uuid=uuid, path=slot_path, branch=branch, blocker=blocker)


def plan_delete(paths: Paths, repo: str, uuid: str) -> DeletePlan:
    """Describe an exact slot deletion without mutating pool or Git state."""
    return _inspect(paths, repo, uuid)


def _remove_registered_worktree(paths: Paths, plan: DeletePlan) -> None:
    main_repo = paths.repo(plan.repo)
    first = git.git(main_repo, "worktree", "remove", str(plan.path), check=False)
    if first.returncode == 0:
        return

    # Clean worktrees containing initialized submodules or ignored build
    # artifacts can require --force. Re-run every safety check immediately
    # before that override so tracked/untracked work is never discarded.
    checked = _inspect(paths, plan.repo, plan.uuid, ignore_owner=_DELETE_OWNER)
    if checked.has_blocker:
        raise CommandError(f"refusing to delete {plan.repo}/{plan.uuid}: {checked.blocker}")
    git.git(main_repo, "worktree", "remove", "--force", str(plan.path))


def delete(paths: Paths, repo: str, uuid: str) -> DeletedSlot:
    """Safely delete one unclaimed pool slot and its Git worktree registration."""
    _require_path_component(repo, "repo")
    _require_path_component(uuid, "UUID")
    pooldb = PoolDB(paths.pool_db())
    try:
        pooldb.claim(repo, uuid, _DELETE_OWNER)
    except SlotBusyError as exc:
        owner = pooldb.get_owner(repo, uuid)
        detail = _owner_blocker(owner) if owner is not None else "slot was claimed concurrently"
        raise CommandError(f"refusing to delete {repo}/{uuid}: {detail}") from exc

    try:
        plan = _inspect(paths, repo, uuid, ignore_owner=_DELETE_OWNER)
        if plan.has_blocker:
            raise CommandError(f"refusing to delete {repo}/{uuid}: {plan.blocker}")
        _remove_registered_worktree(paths, plan)
        return DeletedSlot(repo=repo, uuid=uuid, path=plan.path)
    finally:
        pooldb.release(repo, uuid)
