from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.stacker import gh
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import PRMode, RepoPRConfig, TrackedBranch
from project_manager.stacker.pr import stack_block
from project_manager.stacker.pr.stack_block import _StackRender, render_stack_block
from project_manager.stacker.service import StackerService

from .fakes import RecordingPRBackend


@pytest.fixture
def service(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],  # noqa: ARG001 (creates repo on disk)
) -> StackerService:
    return StackerService(
        StackerDB(pm_env.stacker_db()),
        pm_env,
        pr_backend=RecordingPRBackend(),
    )


def _tracked(branch: str, parent_branch: str) -> TrackedBranch:
    return TrackedBranch(
        repo_name="demo",
        branch=branch,
        parent_repo_name="demo",
        parent_branch=parent_branch,
        managed_base_commit="0" * 40,
        last_synced_parent_commit=None,
        last_clean_head=None,
    )


def _config(mode: PRMode) -> RepoPRConfig:
    return RepoPRConfig(
        repo_name="demo",
        mode=mode,
        trunk_branch="main",
        target_repo="acme/widgets",
    )


def _pr(number: int, branch: str) -> gh.PullRequest:
    return gh.PullRequest(
        number=number,
        url=f"https://github.com/acme/widgets/pull/{number}",
        title=f"PR {number}",
        body="",
        head_ref_name=branch,
        base_ref_name="main",
        state="OPEN",
        is_draft=False,
    )


def _build_ctx(mode: PRMode, service: StackerService) -> tuple[_StackRender, TrackedBranch]:
    parent = _tracked("feat-a", "main")
    child = _tracked("feat-b", "feat-a")
    service.db.upsert_branch(parent)
    service.db.upsert_branch(child)
    component = [parent, child]
    pr_map = {"feat-a": _pr(11, "feat-a"), "feat-b": _pr(12, "feat-b")}
    ctx = _StackRender(
        component=component,
        pr_map=pr_map,
        config=_config(mode),
        current_repo=gh.RepoInfo(name_with_owner="acme/widgets", owner="acme", name="widgets"),
        live_heads={},
    )
    return ctx, child


def test_pr_pr_block_omits_files_link(
    service: StackerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # If `_files_url` is ever called we want the test to fail loudly:
    def _fail(*_args: object, **_kwargs: object) -> str:
        pytest.fail("files URL must not be computed in pr-pr mode")

    monkeypatch.setattr(stack_block, "_files_url", _fail)
    render_ctx, current = _build_ctx("pr-pr", service)
    block = render_stack_block(service.ctx, render_ctx, current)
    assert "Files changed" not in block
    # Preamble is only emitted when files URL is available — pr-pr skips it.
    assert "review incremental changes" not in block
    # Both branches still linked to their PRs via branch name.
    assert "- [feat-a](https://github.com/acme/widgets/pull/11)" in block
    assert "- [**feat-b**](https://github.com/acme/widgets/pull/12)" in block


def test_repo_pr_block_includes_files_link(
    service: StackerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _files(node: TrackedBranch, _ctx: _StackRender) -> str:
        return f"https://github.com/acme/widgets/pull/X/files/abc..def?node={node.branch}"

    monkeypatch.setattr(stack_block, "_files_url", _files)
    render_ctx, current = _build_ctx("repo-pr", service)
    block = render_stack_block(service.ctx, render_ctx, current)
    # Every node gets a "Files changed" link — previously ancestors rendered
    # without one because _compare_url demanded a live worktree checkout.
    assert (
        "[[Files changed](https://github.com/acme/widgets/pull/X/files/abc..def?node=feat-a)]"
        in block
    )
    assert (
        "[[Files changed](https://github.com/acme/widgets/pull/X/files/abc..def?node=feat-b)]"
        in block
    )
    # Intro preamble is intentionally dropped from the gitstack-format block;
    # per-branch [[Files changed]] links already give reviewers per-branch diffs.
    assert "review incremental changes" not in block
