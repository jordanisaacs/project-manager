from __future__ import annotations

import json
from pathlib import Path

from project_manager.stacker.service import StackerService

from .conftest import TrackedStack


def test_ls_default_shows_all_tracked_branches(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    out = service.ls_text(tracked_stack.repo_name)
    for name in ("a", "b", "c", "d"):
        assert name in out


def test_ls_current_scope_narrows_to_lineage(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    out = service.ls_text(
        tracked_stack.repo_name,
        target_branch="b",
        scope="current",
    )
    for name in ("a", "b", "c", "d"):
        assert name in out


def test_ls_details_none_strips_suffixes(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`--details none` emits the tree with no `[synced]` / `[N commits]` markers."""
    out = service.ls_text(tracked_stack.repo_name, details="none")
    assert "[synced]" not in out
    assert "[unsynced]" not in out
    assert "commits]" not in out


def test_ls_details_status_includes_sync_markers(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    out = service.ls_text(tracked_stack.repo_name, details="status")
    assert "[synced]" in out


def test_ls_details_status_counts_includes_commit_counts(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    out = service.ls_text(tracked_stack.repo_name, details="status-counts")
    assert "commits]" in out


def test_ls_json_output_is_parseable(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    raw = service.ls_text(tracked_stack.repo_name, json_output=True)
    entries = json.loads(raw)
    names = {e["branch"] for e in entries}
    assert names == {"a", "b", "c", "d"}
    # status-counts default → managed_base is included on each entry.
    for entry in entries:
        assert "managed_base" in entry


def test_ls_json_details_all_includes_pr_fields(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    raw = service.ls_text(
        tracked_stack.repo_name, details="all", json_output=True,
    )
    entries = json.loads(raw)
    for entry in entries:
        assert "pr_url" in entry
        assert "last_clean_head" in entry


def test_ls_empty_repo_has_no_tracked_branches(
    stacker_repo: tuple[str, Path],  # noqa: ARG001 — seeds pm env
    service: StackerService,
) -> None:
    assert service.ls_text("demo") == "No tracked branches."
