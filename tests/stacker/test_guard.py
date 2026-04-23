from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from project_manager.pool import slot as slot_mod
from project_manager.stacker import git as stacker_git
from project_manager.stacker.models import OperationState, ParentLocator
from project_manager.stacker.service import StackerService


def _initialize(
    service: StackerService, repo_name: str, slot: slot_mod.Slot, branch: str
) -> None:
    service.initialize_worktree(
        repo_name=repo_name,
        worktree_path=slot.path,
        branch=branch,
        create_branch=True,
        parent=ParentLocator(repo_name=repo_name, branch="main"),
    )


def test_guard_no_rebase_blocks_on_tracked_branch(
    monkeypatch: pytest.MonkeyPatch,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, _ = stacker_repo
    slot = three_slots[0]
    _initialize(service, repo_name, slot, "feature-a")
    monkeypatch.chdir(slot.path)
    with pytest.raises(stacker_git.GitError, match="managed by stacker"):
        service.guard_no_rebase()


def test_guard_no_rebase_noop_outside_pm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    service: StackerService,
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-m", "init"],
        check=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@x",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@x",
        },
    )
    monkeypatch.chdir(tmp_path)
    service.guard_no_rebase()


def test_guard_no_rebase_noop_on_detached_head(
    monkeypatch: pytest.MonkeyPatch,
    stacker_repo: tuple[str, Path],  # noqa: ARG001 — fixture seeds pm_env
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    slot = three_slots[0]
    monkeypatch.chdir(slot.path)
    service.guard_no_rebase()


def test_guard_no_rebase_active_op_message(
    monkeypatch: pytest.MonkeyPatch,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, _ = stacker_repo
    slot = three_slots[0]
    _initialize(service, repo_name, slot, "feature-a")
    service.db.put_operation(
        OperationState(
            repo_name=repo_name,
            op_type="local_sync",
            status="paused",
            branch="feature-a",
            parent_branch="main",
        )
    )
    monkeypatch.chdir(slot.path)
    with pytest.raises(stacker_git.GitError, match="active stacker operation"):
        service.guard_no_rebase()
