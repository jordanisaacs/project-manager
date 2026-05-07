from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import add as add_mod
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import Owner, OwnerKind, PoolDB
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import ParentLocator, WorktreeInit
from project_manager.stacker.service import StackerService


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo_path: Path, branch: str = "main") -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo_path)], check=True)
    for key, val in (
        ("user.email", "test@example.com"),
        ("user.name", "test"),
        ("commit.gpgsign", "false"),
    ):
        subprocess.run(["git", "-C", str(repo_path), "config", key, val], check=True)
    (repo_path / "README.md").write_text("# test\n")
    subprocess.run(["git", "-C", str(repo_path), "add", "README.md"], check=True)
    _git("commit", "-q", "-m", "init", cwd=repo_path)


def commit_file(
    path: Path, relpath: str, content: str, message: str
) -> str:
    file_path = path / relpath
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
    _git("add", relpath, cwd=path)
    _git("commit", "-q", "-m", message, cwd=path)
    return _git("rev-parse", "HEAD", cwd=path).stdout.strip()


def claim_forward(paths: Paths, project: str, repo: str, slot: slot_mod.Slot) -> Path:
    """Wire up a project-owned claim on `slot` without going through project.new."""
    (paths.projects / project).mkdir(parents=True, exist_ok=True)
    forward = paths.forward(project, repo)
    forward.symlink_to(slot.path)
    PoolDB(paths.pool_db()).claim(repo, slot.uuid, Owner(OwnerKind.PROJECT, project))
    return forward


@pytest.fixture
def stacker_repo(pm_env: Paths) -> tuple[str, Path]:
    """A minimal bare-but-checked-out pm repo named 'demo' with a 'main' branch."""
    repo = pm_env.repo("demo")
    repo.mkdir()
    _init_repo(repo, branch="main")
    return "demo", repo


@pytest.fixture
def three_slots(pm_env: Paths, stacker_repo: tuple[str, Path]) -> list[slot_mod.Slot]:
    """Three pool slots for 'demo', all free and detached."""
    repo_name, _ = stacker_repo
    return [add_mod.add(pm_env, repo_name) for _ in range(3)]


@pytest.fixture
def service(pm_env: Paths) -> StackerService:
    db = StackerDB(pm_env.stacker_db())
    return StackerService(db, pm_env)


@dataclass(frozen=True)
class TrackedStack:
    """A four-branch chain `main → a → b → c → d`, each in its own slot.

    Each branch has one commit (`<name>.txt`). Use as a starting shape
    for tests that need a real lineage: reparent / split / rename /
    remove / scope resolution all operate against a non-trivial tree.
    """

    repo_name: str
    repo_path: Path
    slots: dict[str, slot_mod.Slot]
    commits: dict[str, str]


def _build_chain(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    chain: list[tuple[str, str]],
    project_label: str,
) -> TrackedStack:
    """Init each `(branch, parent)` in `chain` order in its own claimed slot."""
    repo_name, repo_path = stacker_repo
    service = StackerService(StackerDB(pm_env.stacker_db()), pm_env)
    pooldb = PoolDB(pm_env.pool_db())
    slots: dict[str, slot_mod.Slot] = {}
    commits: dict[str, str] = {}
    for branch, parent in chain:
        slot = add_mod.add(pm_env, repo_name)
        # Real pm claims slots via `pm project new`. The tests that use this
        # fixture want production-accurate ownership so ops_slot.claim cannot
        # grab a slot that already has a branch checked out.
        pooldb.claim(
            repo_name, slot.uuid, Owner(OwnerKind.PROJECT, project_label),
        )
        slots[branch] = slot
        service.init_new_branch(
            WorktreeInit(
                repo_name=repo_name,
                worktree_path=slot.path,
                branch=branch,
                parent=ParentLocator(repo_name=repo_name, branch=parent),
            )
        )
        commits[branch] = commit_file(
            slot.path, f"{branch}.txt", f"{branch}\n", f"{branch}: first commit",
        )
    return TrackedStack(
        repo_name=repo_name,
        repo_path=repo_path,
        slots=slots,
        commits=commits,
    )


@pytest.fixture
def tracked_stack(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
) -> TrackedStack:
    return _build_chain(
        pm_env,
        stacker_repo,
        [("a", "main"), ("b", "a"), ("c", "b"), ("d", "c")],
        "tracked-stack",
    )


@pytest.fixture
def multi_arm_stack(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
) -> TrackedStack:
    """Five-branch multi-arm stack:

        main
          └── a
               ├── b1
               │    └── c1
               └── b2
                    └── c2

    Use as a starting shape for tests that need to exercise default-scope
    walks across sibling arms (push, sync, ls all default to "current"
    scope, which must cover the whole connected component).
    """
    return _build_chain(
        pm_env,
        stacker_repo,
        [
            ("a", "main"),
            ("b1", "a"),
            ("c1", "b1"),
            ("b2", "a"),
            ("c2", "b2"),
        ],
        "multi-arm-stack",
    )
