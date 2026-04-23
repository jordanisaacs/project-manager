"""Upstream helpers read `branch.<X>.remote` / `.merge` from git config.

Stacker deliberately matches universe gitstack's
`remote_branch_for_branch()` semantics: trust the config written by
`git push -u` / `git branch --set-upstream-to`. No `@{upstream}`
rev-parse, no dependency on a materialized remote-tracking ref.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.pool import slot as slot_mod
from project_manager.stacker import git as stacker_git


@pytest.fixture
def slot_on_branch(
    stacker_repo: tuple[str, Path],  # noqa: ARG001 (creates demo repo)
    three_slots: list[slot_mod.Slot],
) -> slot_mod.Slot:
    slot = three_slots[0]
    stacker_git.git(slot.path, "checkout", "-b", "feature-X")
    return slot


def test_all_return_none_when_config_unset(slot_on_branch: slot_mod.Slot) -> None:
    # Slot has a freshly-created branch with no upstream configured yet.
    assert stacker_git.upstream_remote_name(slot_on_branch.path) is None
    assert stacker_git.upstream_branch_name(slot_on_branch.path) is None
    assert stacker_git.upstream_branch(slot_on_branch.path) is None


def test_config_only_is_enough(slot_on_branch: slot_mod.Slot) -> None:
    # No remote-tracking ref exists; we only set the config keys.
    stacker_git.git(slot_on_branch.path, "config", "branch.feature-X.remote", "origin")
    stacker_git.git(
        slot_on_branch.path, "config", "branch.feature-X.merge",
        "refs/heads/user-prefix/feature-X",
    )
    assert stacker_git.upstream_remote_name(slot_on_branch.path) == "origin"
    assert (
        stacker_git.upstream_branch_name(slot_on_branch.path)
        == "user-prefix/feature-X"
    )
    assert (
        stacker_git.upstream_branch(slot_on_branch.path)
        == "origin/user-prefix/feature-X"
    )


def test_malformed_merge_returns_none(slot_on_branch: slot_mod.Slot) -> None:
    # `merge` must start with `refs/heads/`; anything else is ignored.
    stacker_git.git(slot_on_branch.path, "config", "branch.feature-X.remote", "origin")
    stacker_git.git(
        slot_on_branch.path, "config", "branch.feature-X.merge", "refs/tags/v1.0",
    )
    assert stacker_git.upstream_branch_name(slot_on_branch.path) is None
    assert stacker_git.upstream_branch(slot_on_branch.path) is None


@pytest.mark.usefixtures("stacker_repo")
def test_detached_head_returns_none(
    three_slots: list[slot_mod.Slot],
) -> None:
    # three_slots fixture leaves slots detached by default.
    slot = three_slots[0]
    assert stacker_git.upstream_remote_name(slot.path) is None
    assert stacker_git.upstream_branch_name(slot.path) is None
    assert stacker_git.upstream_branch(slot.path) is None
