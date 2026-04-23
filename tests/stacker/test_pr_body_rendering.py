"""End-to-end checks on the PR body `pm stacker pr` actually sends to GitHub.

Uses `RecordingPRBackend` so assertions target the exact bytes we would
have written to `gh pr create --body-file` / `gh api POST /repos/.../pulls`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.stacker import git as stacker_git
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import ParentLocator, SelectorTarget
from project_manager.stacker.service import StackerService

from .fakes import RecordingPRBackend


def _set_config(service: StackerService, repo: str, mode: str) -> None:
    service.db.set_config(repo, "pr.mode", mode)
    service.db.set_config(repo, "pr.trunk", "main")


def _build_branch(  # noqa: PLR0913 (test helper)
    repo_path: Path, slot_path: Path, branch: str, parent: str,
    content: str, commit_msg: str,
) -> None:
    """Create `branch` off `parent`'s tip and add one commit."""
    parent_head = stacker_git.rev_parse(repo_path, parent)
    stacker_git.git(slot_path, "checkout", "-b", branch, parent_head)
    (slot_path / f"{branch}.txt").write_text(content)
    stacker_git.git(slot_path, "add", f"{branch}.txt")
    stacker_git.git(slot_path, "-c", "user.email=t@e.com", "-c", "user.name=t",
                    "commit", "-m", commit_msg)


def _setup_fake_upstream(slot_path: Path, branch: str) -> None:
    """Configure `branch.<X>.remote/merge` as `git push -u` would.

    Stacker reads these config keys directly (mirroring universe
    gitstack's `remote_branch_for_branch()`), so no real push or
    remote-tracking ref is needed.
    """
    stacker_git.git(slot_path, "remote", "add", "origin-fake",
                    "git@github.com:acme/widgets.git", check=False)
    stacker_git.git(slot_path, "config", f"branch.{branch}.remote", "origin-fake")
    stacker_git.git(slot_path, "config", f"branch.{branch}.merge",
                    f"refs/heads/{branch}")


@pytest.fixture
def backend() -> RecordingPRBackend:
    return RecordingPRBackend()


@pytest.fixture
def service(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],  # noqa: ARG001 (creates demo repo)
    backend: RecordingPRBackend,
) -> StackerService:
    return StackerService(
        StackerDB(pm_env.stacker_db()), pm_env, pr_backend=backend,
    )


def _two_branch_stack(
    service: StackerService,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str, slot_mod.Slot, slot_mod.Slot]:
    repo_name, repo_path = stacker_repo
    slot_a, slot_b = three_slots[0], three_slots[1]

    _build_branch(repo_path, slot_a.path, "feature-a", "main",
                  "A\n", "A: first commit\n\nExtra body for A.")
    _setup_fake_upstream(slot_a.path, "feature-a")
    service.initialize_worktree(
        repo_name=repo_name, worktree_path=slot_a.path, branch="feature-a",
        create_branch=False, parent=ParentLocator(repo_name=repo_name, branch="main"),
    )

    _build_branch(repo_path, slot_b.path, "feature-b", "feature-a",
                  "B\n", "B: second commit\n\nExtra body for B.")
    _setup_fake_upstream(slot_b.path, "feature-b")
    service.initialize_worktree(
        repo_name=repo_name, worktree_path=slot_b.path, branch="feature-b",
        create_branch=False,
        parent=ParentLocator(repo_name=repo_name, branch="feature-a"),
    )

    # pp uses `git pp --force` (a databricks alias not in test env) — stub.
    monkeypatch.setattr(service, "_run_single_pp", lambda *_a, **_kw: True)
    return repo_name, "feature-b", slot_a, slot_b


def test_pr_body_has_actual_commit_message_not_file_path(
    service: StackerService,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: graphql/REST paths must send file content, never `@path`.

    Real universe runs showed both PR bodies contained the literal string
    `@/tmp/tmpXXXXX` because we passed `body=@file` to `gh api graphql`
    which only substitutes file refs on REST, not on graphql variables.
    """
    repo_name, _leaf, _a_slot, _b_slot = _two_branch_stack(
        service, stacker_repo, three_slots, monkeypatch,
    )
    _set_config(service, repo_name, "repo-pr")

    service.pr(SelectorTarget(repo_name=repo_name, branch="feature-b"), draft=True)

    assert len(backend.created) == 2, "both ancestor and leaf get PRs"
    for request, body in backend.created:
        assert not body.startswith("@/"), f"literal @path in body for {request.head}"
        assert "tmp" not in body.splitlines()[0], f"looks like a tmp path: {body[:80]!r}"


def test_pr_body_includes_first_commit_body_text(
    service: StackerService,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, _leaf, _a_slot, _b_slot = _two_branch_stack(
        service, stacker_repo, three_slots, monkeypatch,
    )
    _set_config(service, repo_name, "repo-pr")

    service.pr(SelectorTarget(repo_name=repo_name, branch="feature-b"), draft=True)

    # The body-at-create is the first-commit body (no stack block yet —
    # refresh follows). "Extra body" is in the commit trailer of both.
    bodies_by_branch = {req.head: body for (req, body) in backend.created}
    assert "Extra body for A." in bodies_by_branch["feature-a"]
    assert "Extra body for B." in bodies_by_branch["feature-b"]


def test_refreshed_stack_block_links_both_prs_even_when_list_is_empty(
    service: StackerService,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: PR A's link must be present in PR B's stack block.

    Real universe PR #1845321 showed the ancestor rendered as a bare
    branch name because `gh pr list` hadn't indexed the just-created PR A.
    Fix: preload the PRs we just created into _refresh_component_pr_bodies.
    """
    repo_name, _leaf, _a_slot, _b_slot = _two_branch_stack(
        service, stacker_repo, three_slots, monkeypatch,
    )
    _set_config(service, repo_name, "repo-pr")
    # Simulate the race: list_open_prs returns nothing (GitHub index lag).
    backend.prs_by_head.clear()

    service.pr(SelectorTarget(repo_name=repo_name, branch="feature-b"), draft=True)

    # After pr(), refresh wrote each PR's final body via edit_pr(body_file=...).
    # Pull the most recent body written for each PR number.
    final_body_for_number: dict[int, str] = {}
    for request, body in backend.edited:
        if body is not None:
            final_body_for_number[request.number] = body

    # PR #1 = feature-a, PR #2 = feature-b (numbers assigned by fake).
    body_a = final_body_for_number[1]
    body_b = final_body_for_number[2]

    # Both stack blocks must contain links to both PRs. The universe
    # regression we're guarding against was the ancestor line rendered as
    # a bare branch name with no PR link — catching [PR #N]( in each body
    # for both ancestor and leaf is exactly the property that was broken.
    for label, body in [("PR A", body_a), ("PR B", body_b)]:
        assert "[PR #1](" in body, f"{label} is missing ancestor PR link"
        assert "[PR #2](" in body, f"{label} is missing leaf PR link"


def test_repo_pr_includes_compare_url_in_block(
    service: StackerService,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, _leaf, _a_slot, _b_slot = _two_branch_stack(
        service, stacker_repo, three_slots, monkeypatch,
    )
    _set_config(service, repo_name, "repo-pr")

    service.pr(SelectorTarget(repo_name=repo_name, branch="feature-b"), draft=True)

    final = {r.number: b for (r, b) in backend.edited if b is not None}
    body_b = final[2]
    # Files-changed URL format: <pr-url>/changes/<base>..<head>
    # (matches universe gitstack's <pr>/files/<parent>..<head>).
    assert "/pull/2/changes/" in body_b
    assert "[changes](" in body_b


def test_pr_pr_mode_stack_block_omits_compare_link(
    service: StackerService,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, _leaf, _a_slot, _b_slot = _two_branch_stack(
        service, stacker_repo, three_slots, monkeypatch,
    )
    _set_config(service, repo_name, "pr-pr")

    service.pr(SelectorTarget(repo_name=repo_name, branch="feature-b"), draft=True)

    final = {r.number: b for (r, b) in backend.edited if b is not None}
    assert 2 in final, "leaf body should have been edited to add stack block"
    body_b = final[2]
    assert "/compare/" not in body_b, "pr-pr mode must not embed compare URLs"
    assert "[changes](" not in body_b


def test_head_repo_set_when_upstream_is_distinct_fork(
    service: StackerService,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If branch.<X>.remote URL parses to a different slug than the PR
    target, CreatePRRequest.head_repo must carry that slug so create_pr
    takes the REST path. The default RecordingPRBackend reports
    name_with_owner=acme/widgets, matching the stub URL we set — so
    head_repo should be populated and equal to acme/widgets."""
    repo_name, _leaf, _a_slot, _b_slot = _two_branch_stack(
        service, stacker_repo, three_slots, monkeypatch,
    )
    _set_config(service, repo_name, "repo-pr")
    service.pr(SelectorTarget(repo_name=repo_name, branch="feature-b"), draft=True)
    for request, _body in backend.created:
        assert request.head_repo == "acme/widgets"
