"""End-to-end checks on the PR body `pm stacker pr` actually sends to GitHub.

Uses `RecordingPRBackend` so assertions target the exact bytes we would
have written to `gh pr create --body-file` / `gh api POST /repos/.../pulls`.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.stacker import git as stacker_git
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import (
    ParentLocator,
    PushOptions,
    SelectorTarget,
    WorktreeInit,
)
from project_manager.stacker.ops import worktree as ops_worktree
from project_manager.stacker.service import StackerService

from .conftest import commit_file
from .fakes import RecordingPRBackend


def _set_config(service: StackerService, repo: str, mode: str) -> None:
    service.db.set_config(repo, "pr.mode", mode)
    service.db.set_config(repo, "pr.trunk", "main")


def _build_branch(
    repo_path: Path,
    slot_path: Path,
    branch: str,
    parent: str,
    commit: tuple[str, str],
) -> None:
    """Create `branch` off `parent`'s tip and add one commit.

    `commit` is `(file_content, commit_message)`.
    """
    content, commit_msg = commit
    parent_head = stacker_git.rev_parse(repo_path, parent)
    stacker_git.git(slot_path, "checkout", "-b", branch, parent_head)
    (slot_path / f"{branch}.txt").write_text(content)
    stacker_git.git(slot_path, "add", f"{branch}.txt")
    stacker_git.git(
        slot_path, "-c", "user.email=t@e.com", "-c", "user.name=t", "commit", "-m", commit_msg
    )


def _setup_fake_upstream(slot_path: Path, branch: str) -> None:
    """Configure `branch.<X>.remote/merge` as `git push -u` would.

    Stacker reads these config keys directly, so no real push or
    remote-tracking ref is needed.
    """
    stacker_git.git(
        slot_path, "remote", "add", "origin-fake", "git@github.com:acme/widgets.git", check=False
    )
    stacker_git.git(slot_path, "config", f"branch.{branch}.remote", "origin-fake")
    stacker_git.git(slot_path, "config", f"branch.{branch}.merge", f"refs/heads/{branch}")


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
        StackerDB(pm_env.stacker_db()),
        pm_env,
        pr_backend=backend,
    )


def _two_branch_stack(
    service: StackerService,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str, slot_mod.Slot, slot_mod.Slot]:
    repo_name, repo_path = stacker_repo
    slot_a, slot_b = three_slots[0], three_slots[1]

    _build_branch(
        repo_path, slot_a.path, "feature-a", "main", ("A\n", "A: first commit\n\nExtra body for A.")
    )
    _setup_fake_upstream(slot_a.path, "feature-a")
    service.init_adopt_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=slot_a.path,
            branch="feature-a",
            parent=ParentLocator(repo_name=repo_name, branch="main"),
        )
    )

    _build_branch(
        repo_path,
        slot_b.path,
        "feature-b",
        "feature-a",
        ("B\n", "B: second commit\n\nExtra body for B."),
    )
    _setup_fake_upstream(slot_b.path, "feature-b")
    service.init_adopt_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=slot_b.path,
            branch="feature-b",
            parent=ParentLocator(repo_name=repo_name, branch="feature-a"),
        )
    )

    # pp uses `git pp --force` (user push alias not in test env) — stub.
    monkeypatch.setattr(ops_worktree, "run_single_pp", lambda *_a, **_kw: True)
    return repo_name, "feature-b", slot_a, slot_b


def test_pr_body_has_actual_commit_message_not_file_path(
    service: StackerService,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: graphql/REST paths must send file content, never `@path`.

    A prior incident saw both PR bodies contain the literal string
    `@/tmp/tmpXXXXX` because we passed `body=@file` to `gh api graphql`
    which only substitutes file refs on REST, not on graphql variables.
    """
    repo_name, _leaf, _a_slot, _b_slot = _two_branch_stack(
        service,
        stacker_repo,
        three_slots,
        monkeypatch,
    )
    _set_config(service, repo_name, "repo-pr")

    service.push(SelectorTarget(repo_name=repo_name, branch="feature-b"), PushOptions(draft=True))

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
        service,
        stacker_repo,
        three_slots,
        monkeypatch,
    )
    _set_config(service, repo_name, "repo-pr")

    service.push(SelectorTarget(repo_name=repo_name, branch="feature-b"), PushOptions(draft=True))

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

    A prior incident saw the ancestor rendered as a bare branch name
    because `gh pr list` hadn't indexed the just-created PR A.
    Fix: preload the PRs we just created into _refresh_component_pr_bodies.
    """
    repo_name, _leaf, _a_slot, _b_slot = _two_branch_stack(
        service,
        stacker_repo,
        three_slots,
        monkeypatch,
    )
    _set_config(service, repo_name, "repo-pr")
    # Simulate the race: list_open_prs returns nothing (GitHub index lag).
    backend.prs_by_head.clear()

    service.push(SelectorTarget(repo_name=repo_name, branch="feature-b"), PushOptions(draft=True))

    # After pr(), refresh wrote each PR's final body via edit_pr(body_file=...).
    # Pull the most recent body written for each PR number.
    final_body_for_number: dict[int, str] = {}
    for request, body in backend.edited:
        if body is not None:
            final_body_for_number[request.number] = body

    # PR #1 = feature-a, PR #2 = feature-b (numbers assigned by fake).
    body_a = final_body_for_number[1]
    body_b = final_body_for_number[2]

    # Both stack blocks must contain links to both PRs. The regression
    # we're guarding against was the ancestor line rendered as a bare
    # branch name with no PR link — catching each branch linked to its
    # PR URL (via the `[branch](pr-url)` form) proves the race is closed.
    for label, body in [("PR A", body_a), ("PR B", body_b)]:
        assert "](https://github.com/acme/widgets/pull/1)" in body, f"{label} missing link to PR A"
        assert "](https://github.com/acme/widgets/pull/2)" in body, f"{label} missing link to PR B"


def test_repo_pr_includes_compare_url_in_block(
    service: StackerService,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, _leaf, _a_slot, _b_slot = _two_branch_stack(
        service,
        stacker_repo,
        three_slots,
        monkeypatch,
    )
    _set_config(service, repo_name, "repo-pr")

    service.push(SelectorTarget(repo_name=repo_name, branch="feature-b"), PushOptions(draft=True))

    final = {r.number: b for (r, b) in backend.edited if b is not None}
    body_b = final[2]
    # Files-changed URL format: <pr-url>/files/<base>..<head>.
    assert "/pull/2/files/" in body_b
    assert "[[Files changed](" in body_b


def test_repo_pr_files_url_uses_current_head_after_local_commit(
    service: StackerService,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: `Files changed` head SHA must reflect the pushed HEAD,
    not the stale `last_clean_head` written at init time.

    A prior incident saw two PRs render `/files/<sha>..<sha>` — an empty
    diff — because after `pm stacker init` seeded
    `last_clean_head = parent_head`, a follow-up local commit advanced
    HEAD but `pm stacker pp` never refreshed `last_clean_head` in the
    DB before rendering the stack block.
    """
    repo_name, _leaf, _slot_a, slot_b = _two_branch_stack(
        service,
        stacker_repo,
        three_slots,
        monkeypatch,
    )
    _set_config(service, repo_name, "repo-pr")

    # Advance feature-b's HEAD past the value `init_adopt_branch` recorded
    # in `last_clean_head`. This is the user-perspective scenario: branch
    # was tracked, then more local commits landed before pp.
    (slot_b.path / "feature-b-extra.txt").write_text("more\n")
    stacker_git.git(slot_b.path, "add", "feature-b-extra.txt")
    stacker_git.git(
        slot_b.path,
        "-c", "user.email=t@e.com", "-c", "user.name=t",
        "commit", "-m", "B: extra local commit",
    )
    actual_head_b = stacker_git.rev_parse(slot_b.path, "HEAD")

    service.push(SelectorTarget(repo_name=repo_name, branch="feature-b"), PushOptions(draft=True))

    final = {r.number: b for (r, b) in backend.edited if b is not None}
    body_b = final[2]
    match = re.search(r"/pull/2/files/([0-9a-f]+)\.\.([0-9a-f]+)", body_b)
    assert match is not None, f"Files-changed URL missing from body: {body_b!r}"
    base_sha, head_sha = match.group(1), match.group(2)
    assert head_sha == actual_head_b, (
        f"head SHA in stack block ({head_sha}) does not match feature-b's "
        f"current HEAD ({actual_head_b}); last_clean_head was not refreshed "
        f"after the local commit"
    )
    assert base_sha != head_sha, (
        f"base..head collapsed to {base_sha}..{head_sha} (empty diff)"
    )


def test_repo_pr_root_branch_files_link_omits_range(
    service: StackerService,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: branches whose parent is the trunk must render
    `<pr>/files` (no range) — the base SHA is a trunk commit that lives
    outside the PR's commit graph, so GitHub 404s a `/files/<base>..<head>`
    URL. Suppress the range form when `parent == default_branch`.

    A prior incident on a root-of-stack branch surfaced this: GitHub
    returned 404 even after we fixed the head SHA staleness — the URL
    itself was structurally wrong for a root branch.
    """
    repo_name, _leaf, _slot_a, _slot_b = _two_branch_stack(
        service,
        stacker_repo,
        three_slots,
        monkeypatch,
    )
    _set_config(service, repo_name, "repo-pr")

    service.push(SelectorTarget(repo_name=repo_name, branch="feature-b"), PushOptions(draft=True))

    final = {r.number: b for (r, b) in backend.edited if b is not None}
    body_a = final[1]
    # feature-a's self-line in its own stack block. Should link to /files,
    # not /files/<sha>..<sha>.
    assert re.search(
        r"\[\*\*feature-a\*\*\]\(https://github\.com/acme/widgets/pull/1\) "
        r"\[\[Files changed\]\(https://github\.com/acme/widgets/pull/1/files\)\]",
        body_a,
    ), f"feature-a (root, parent=trunk) must render /files without a range: {body_a!r}"

    # The body of PR B should also render feature-a's ancestor row without
    # a range, since the rule is per-branch (depends on its parent).
    body_b = final[2]
    assert re.search(
        r"\[feature-a\]\(https://github\.com/acme/widgets/pull/1\) "
        r"\[\[Files changed\]\(https://github\.com/acme/widgets/pull/1/files\)\]",
        body_b,
    ), f"feature-a row in PR B must also use /files without a range: {body_b!r}"


def test_pr_pr_mode_stack_block_omits_compare_link(
    service: StackerService,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, _leaf, _a_slot, _b_slot = _two_branch_stack(
        service,
        stacker_repo,
        three_slots,
        monkeypatch,
    )
    _set_config(service, repo_name, "pr-pr")

    service.push(SelectorTarget(repo_name=repo_name, branch="feature-b"), PushOptions(draft=True))

    final = {r.number: b for (r, b) in backend.edited if b is not None}
    assert 2 in final, "leaf body should have been edited to add stack block"
    body_b = final[2]
    assert "Files changed" not in body_b, "pr-pr mode must not embed files URLs"
    assert "review incremental changes" not in body_b


def test_second_push_preserves_user_edits_to_pr_body(
    service: StackerService,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: editing the PR description and re-running `pm stacker push`
    must not overwrite the user's prose with the first-commit body.

    A prior incident saw the user fill in the repo's
    PULL_REQUEST_TEMPLATE.md sections, then a follow-up `pm stacker push`
    silently reset the body to the first commit message.
    """
    repo_name, _leaf, _a_slot, _b_slot = _two_branch_stack(
        service,
        stacker_repo,
        three_slots,
        monkeypatch,
    )
    _set_config(service, repo_name, "repo-pr")
    target = SelectorTarget(repo_name=repo_name, branch="feature-b")

    service.push(target, PushOptions(draft=True))

    # Simulate the user editing PR B's description in the GitHub UI: replace
    # the body with template prose. view_pr() reads back from prs_by_head,
    # so the next push sees this body instead of the first-commit message.
    user_body = (
        "## What did you change, and why?\n\n"
        "Filled in by the human, must not be clobbered.\n\n"
        "## How do you know it works?\n\n"
        "Tested in dev.\n"
    )
    key = (backend.default_repo.name_with_owner, "feature-b")
    backend.prs_by_head[key] = replace(backend.prs_by_head[key], body=user_body)
    edits_before_second_push = len(backend.edited)

    service.push(target, PushOptions(draft=True))

    # create_or_update_current_pr emits the first edit_pr per branch on a
    # re-push; that call must NOT carry a body_file. Otherwise it
    # overwrites the user-edited description with the first-commit body
    # before the refresh step ever sees it.
    second_push_edits = backend.edited[edits_before_second_push:]
    assert second_push_edits, "second push should hit edit_pr for the existing PR"
    update_requests_with_body = [
        req for req, body in second_push_edits if body is not None and req.number == 2
    ]
    # Exactly one body-bearing edit_pr per branch — the refresh-stack-block
    # write. The create-or-update step on an existing PR must skip body.
    assert len(update_requests_with_body) == 1, (
        "create_or_update_current_pr must not write a body for an existing PR"
    )

    # The refresh that follows reads the (still user-edited) body, swaps
    # only the managed block, and writes it back. The user's prose
    # survives end-to-end.
    final_body_for_pr_b = next(
        body for req, body in reversed(backend.edited) if req.number == 2 and body is not None
    )
    assert "Filled in by the human, must not be clobbered." in final_body_for_pr_b
    assert "<!-- stacker:begin -->" in final_body_for_pr_b
    assert "<!-- stacker:end -->" in final_body_for_pr_b


def test_push_from_middle_branch_links_focus_pr_in_stack_blocks(
    service: StackerService,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pushing from a non-leaf focus must link the focus's own PR everywhere.

    A prior incident: a 3-branch stack pushed from the middle wrote the
    leaf's PR URL into every line referencing the middle branch. The
    fix is that `record_pr` (called per-iteration inside
    `create_or_update_current_pr`) keeps the cache fresh, so a later
    `pr_map_for_component` lookup resolves each branch to its own PR
    without needing an override that assumed the focus was the
    last-processed branch.
    """
    repo_name, repo_path = stacker_repo
    slot_a, slot_b, slot_c = three_slots[0], three_slots[1], three_slots[2]

    _build_branch(
        repo_path, slot_a.path, "feature-a", "main", ("A\n", "A: first commit\n\n"),
    )
    _setup_fake_upstream(slot_a.path, "feature-a")
    service.init_adopt_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=slot_a.path,
            branch="feature-a",
            parent=ParentLocator(repo_name=repo_name, branch="main"),
        )
    )

    _build_branch(
        repo_path, slot_b.path, "feature-b", "feature-a", ("B\n", "B: second commit\n\n"),
    )
    _setup_fake_upstream(slot_b.path, "feature-b")
    service.init_adopt_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=slot_b.path,
            branch="feature-b",
            parent=ParentLocator(repo_name=repo_name, branch="feature-a"),
        )
    )

    _build_branch(
        repo_path, slot_c.path, "feature-c", "feature-b", ("C\n", "C: third commit\n\n"),
    )
    _setup_fake_upstream(slot_c.path, "feature-c")
    service.init_adopt_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=slot_c.path,
            branch="feature-c",
            parent=ParentLocator(repo_name=repo_name, branch="feature-b"),
        )
    )

    monkeypatch.setattr(ops_worktree, "run_single_pp", lambda *_a, **_kw: True)
    _set_config(service, repo_name, "repo-pr")

    # Default scope walks the full lineage parent→leaf, so PR #1 (feature-a),
    # PR #2 (feature-b), PR #3 (feature-c) get created in that order.
    service.push(
        SelectorTarget(repo_name=repo_name, branch="feature-b"), PushOptions(draft=True),
    )

    final = {req.number: body for (req, body) in backend.edited if body is not None}
    assert {1, 2, 3} <= final.keys(), (
        "every branch's body must be refreshed; if PR #2 is missing, "
        "the focus PR was overwritten in pr_map and skipped its body update"
    )

    pattern = re.compile(
        r"\[\*?\*?feature-b\*?\*?\]\((https://github\.com/acme/widgets/pull/\d+)\)"
    )
    for label, number in [("PR A", 1), ("PR B", 2), ("PR C", 3)]:
        body = final[number]
        match = pattern.search(body)
        assert match, f"{label} body missing feature-b link: {body!r}"
        assert match.group(1) == "https://github.com/acme/widgets/pull/2", (
            f"{label} renders feature-b → {match.group(1)}, expected /pull/2"
        )


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
        service,
        stacker_repo,
        three_slots,
        monkeypatch,
    )
    _set_config(service, repo_name, "repo-pr")
    service.push(SelectorTarget(repo_name=repo_name, branch="feature-b"), PushOptions(draft=True))
    for request, _body in backend.created:
        assert request.head_repo == "acme/widgets"


def _seed_pr_template(repo_path: Path, content: str) -> None:
    """Commit `.github/PULL_REQUEST_TEMPLATE.md` to main so slot worktrees inherit it.

    `_build_branch` does `git checkout -b <branch> <main_head>` in each
    slot, which updates the slot's working tree to match `main` — so the
    template lands in every slot before pushes call `load_pr_template`.
    """
    commit_file(repo_path, ".github/PULL_REQUEST_TEMPLATE.md", content, "add PR template")


def _user_section(body: str) -> str:
    """Slice everything after the managed block end marker."""
    marker = "<!-- stacker:end -->"
    idx = body.index(marker)
    return body[idx + len(marker) :].lstrip("\n")


def _final_body_for(backend: RecordingPRBackend, branch: str) -> str:
    """Last body the backend wrote for `branch` (after refresh appends the block)."""
    pr_number = next(
        pr.number for (_repo, head), pr in backend.prs_by_head.items() if head == branch
    )
    for req, body in reversed(backend.edited):
        if req.number == pr_number and body is not None:
            return body
    raise AssertionError(f"no body-bearing edit_pr for {branch}")


def test_create_pr_uses_template_when_present(
    service: StackerService,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, repo_path = stacker_repo
    _seed_pr_template(repo_path, "## Summary\n\n## Test plan\n")
    _two_branch_stack(service, stacker_repo, three_slots, monkeypatch)
    _set_config(service, repo_name, "repo-pr")

    service.push(SelectorTarget(repo_name=repo_name, branch="feature-b"), PushOptions(draft=True))

    for branch, expected_extra in (("feature-a", "Extra body for A."), ("feature-b", "Extra body for B.")):
        body = _final_body_for(backend, branch)
        user = _user_section(body)
        assert "## Summary" in user
        assert "## Test plan" in user
        assert expected_extra in user
        # Commit body sits between the two template headers.
        assert user.index("## Summary") < user.index(expected_extra) < user.index("## Test plan")


def test_create_pr_no_template_unchanged_user_section(
    service: StackerService,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backwards-compat guard: no template → user content is just the commit body."""
    repo_name, _repo_path = stacker_repo
    _two_branch_stack(service, stacker_repo, three_slots, monkeypatch)
    _set_config(service, repo_name, "repo-pr")

    service.push(SelectorTarget(repo_name=repo_name, branch="feature-b"), PushOptions(draft=True))

    body_a = _final_body_for(backend, "feature-a")
    assert _user_section(body_a) == "Extra body for A."


def test_create_pr_template_without_headers_prepends_commit_body(
    service: StackerService,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_name, repo_path = stacker_repo
    _seed_pr_template(repo_path, "Boilerplate prose.\n")
    _two_branch_stack(service, stacker_repo, three_slots, monkeypatch)
    _set_config(service, repo_name, "repo-pr")

    service.push(SelectorTarget(repo_name=repo_name, branch="feature-b"), PushOptions(draft=True))

    body_a = _final_body_for(backend, "feature-a")
    user = _user_section(body_a)
    assert user.startswith("Extra body for A.")
    assert "Boilerplate prose." in user


def test_second_push_preserves_user_edits_to_template_filled_body(
    service: StackerService,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User-edit-survives-push regression — extended for templates.

    With template support seeding the initial body, the user editing it
    in the GitHub UI must still survive re-push. Guard: re-push must
    emit exactly one body-bearing edit_pr per branch (the stack-block
    refresh), and the user's edits below the managed block survive.
    """
    repo_name, repo_path = stacker_repo
    _seed_pr_template(repo_path, "## Summary\n\n## Test plan\n")
    _two_branch_stack(service, stacker_repo, three_slots, monkeypatch)
    _set_config(service, repo_name, "repo-pr")
    target = SelectorTarget(repo_name=repo_name, branch="feature-b")

    service.push(target, PushOptions(draft=True))

    # User edits the body in the GitHub UI to fill in template sections.
    user_body = (
        "## Summary\n\nFilled in by the human, must not be clobbered.\n\n"
        "## Test plan\n\nTested in dev.\n"
    )
    key = (backend.default_repo.name_with_owner, "feature-b")
    backend.prs_by_head[key] = replace(backend.prs_by_head[key], body=user_body)
    edits_before_second_push = len(backend.edited)

    service.push(target, PushOptions(draft=True))

    second_push_edits = backend.edited[edits_before_second_push:]
    update_with_body = [
        req for req, body in second_push_edits if body is not None and req.number == 2
    ]
    assert len(update_with_body) == 1, (
        "create_or_update_current_pr must not re-write body for an existing PR"
    )
    final_b = next(
        body for req, body in reversed(backend.edited) if req.number == 2 and body is not None
    )
    assert "Filled in by the human, must not be clobbered." in final_b
    assert "<!-- stacker:begin -->" in final_b
    assert "<!-- stacker:end -->" in final_b
