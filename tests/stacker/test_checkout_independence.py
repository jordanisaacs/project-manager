from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.cli._shared import RepoFlag
from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.stacker import git, locate
from project_manager.stacker.commands.log import log as cli_log
from project_manager.stacker.models import ParentLocator, SelectorTarget, SyncOptions, WorktreeInit
from project_manager.stacker.service import StackerService
from tests.stacker.conftest import commit_file


def _tracked_feature(
    service: StackerService,
    repo_name: str,
    slot: slot_mod.Slot,
) -> str:
    service.init_new_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=slot.path,
            branch="feature",
            parent=ParentLocator(repo_name=repo_name, branch="main"),
        )
    )
    return commit_file(slot.path, "feature.txt", "feature\n", "feature commit")


@pytest.mark.parametrize("checkout_state", ["other-branch", "detached"])
def test_log_explicit_branch_does_not_require_target_checkout(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
    capsys: pytest.CaptureFixture[str],
    checkout_state: str,
) -> None:
    repo_name, _ = stacker_repo
    slot = three_slots[0]
    _tracked_feature(service, repo_name, slot)
    if checkout_state == "other-branch":
        git.checkout(slot.path, "-b", "other", "main")
    else:
        git.detach_head(slot.path)
    assert locate.locate_worktree(service.paths, repo_name, "feature") is None

    assert cli_log(branch="feature", flag=RepoFlag(repo=repo_name)) == 0

    output = capsys.readouterr().out
    assert "feature commit" in output
    assert f"{repo_name}:feature commits since parent" in output


def test_log_explicit_branch_infers_repo_from_detached_cwd_slot(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, _ = stacker_repo
    slot = three_slots[0]
    _tracked_feature(service, repo_name, slot)
    git.detach_head(slot.path)
    monkeypatch.chdir(slot.path)

    assert cli_log(branch="feature", flag=RepoFlag()) == 0


def test_repair_explicit_branch_resolves_head_relative_ref_without_checkout(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    slot = three_slots[0]
    feature_head = _tracked_feature(service, repo_name, slot)
    git.checkout(slot.path, "-b", "other", "main")
    assert locate.locate_worktree(pm_env, repo_name, "feature") is None

    result = service.repair(SelectorTarget(repo_name, "feature"), "HEAD~1")

    repaired = service.db.get_branch(repo_name, "feature")
    assert repaired is not None
    assert repaired.managed_base_commit == git.rev_parse(repo_path, "main")
    assert repaired.last_clean_head == feature_head
    assert "Repaired" in result


def test_repair_still_refuses_dirty_live_target_checkout(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, _ = stacker_repo
    slot = three_slots[0]
    _tracked_feature(service, repo_name, slot)
    (slot.path / "feature.txt").write_text("dirty\n")

    with pytest.raises(git.GitError, match="Tracked changes"):
        service.repair(SelectorTarget(repo_name, "feature"), "main")


def test_sync_can_continue_after_post_cherry_pick_submodule_update_failure(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, repo_path = stacker_repo
    slot = three_slots[0]
    _tracked_feature(service, repo_name, slot)
    commit_file(repo_path, "parent.txt", "parent\n", "advance parent")
    real_update = git.update_submodules
    calls = 0

    def fail_second_update(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise git.GitError("simulated submodule checkout failure")
        real_update(path)

    monkeypatch.setattr(git, "update_submodules", fail_second_update)
    paused = service.sync(
        SelectorTarget(repo_name, "feature"),
        options=SyncOptions(offline=True),
    )

    assert "paused" in paused.lower()
    assert "submodule update failed after cherry-pick completed" in paused
    op = service.db.get_operation(repo_name)
    assert op is not None
    assert op.status == "paused"

    finished = service.continue_operation(repo_name)
    assert "Sync complete." in finished
    assert service.db.get_operation(repo_name) is None
