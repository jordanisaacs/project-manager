from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.stacker import gh
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import PushOptions, SelectorTarget, TrackedBranch
from project_manager.stacker.ops import push as push_ops
from project_manager.stacker.ops.worktree import _Acquired
from project_manager.stacker.pr import create_update as pr_create_update
from project_manager.stacker.pr.lineage import ancestor_chain
from project_manager.stacker.service import StackerService

from .fakes import RecordingPRBackend


@pytest.fixture
def service(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],  # noqa: ARG001 (creates demo repo)
) -> StackerService:
    return StackerService(
        StackerDB(pm_env.stacker_db()),
        pm_env,
        pr_backend=RecordingPRBackend(),
    )


def _tracked(branch: str, parent: str) -> TrackedBranch:
    return TrackedBranch(
        repo_name="demo",
        branch=branch,
        parent_repo_name="demo",
        parent_branch=parent,
        managed_base_commit="0" * 40,
        last_synced_parent_commit=None,
        last_clean_head=None,
    )


def test_chain_single_branch_with_untracked_parent(service: StackerService) -> None:
    leaf = _tracked("feature-a", "main")
    service.db.upsert_branch(leaf)
    chain = ancestor_chain(service.ctx, leaf)
    # main is the trunk and not stacker-tracked, so the chain is just the leaf.
    assert [b.branch for b in chain] == ["feature-a"]


def test_chain_two_tracked_levels_root_first(service: StackerService) -> None:
    parent = _tracked("feature-a", "main")
    leaf = _tracked("feature-b", "feature-a")
    service.db.upsert_branch(parent)
    service.db.upsert_branch(leaf)
    chain = ancestor_chain(service.ctx, leaf)
    assert [b.branch for b in chain] == ["feature-a", "feature-b"]


def test_chain_stops_at_untracked_parent_mid_stack(service: StackerService) -> None:
    # Only feature-b is tracked; its parent feature-a is not. Chain is just [b].
    leaf = _tracked("feature-b", "feature-a")
    service.db.upsert_branch(leaf)
    chain = ancestor_chain(service.ctx, leaf)
    assert [b.branch for b in chain] == ["feature-b"]


def test_chain_three_levels(service: StackerService) -> None:
    a = _tracked("feature-a", "main")
    b = _tracked("feature-b", "feature-a")
    c = _tracked("feature-c", "feature-b")
    service.db.upsert_branch(a)
    service.db.upsert_branch(b)
    service.db.upsert_branch(c)
    chain = ancestor_chain(service.ctx, c)
    assert [x.branch for x in chain] == ["feature-a", "feature-b", "feature-c"]


def test_pr_walks_ancestors_root_first(
    service: StackerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`pr(leaf)` should push+create-PR for every tracked ancestor first."""
    a = _tracked("feature-a", "main")
    b = _tracked("feature-b", "feature-a")
    c = _tracked("feature-c", "feature-b")
    for tb in (a, b, c):
        service.db.upsert_branch(tb)

    pp_order: list[str] = []
    pr_order: list[str] = []

    def _fake_pp(_ctx: object, tb: TrackedBranch, *_args: object, **_kwargs: object) -> bool:
        pp_order.append(tb.branch)
        return True

    @contextlib.contextmanager
    def _fake_acquired_for_op(_ctx: object, _repo: str, _branch: str) -> Iterator[_Acquired]:
        yield _Acquired(path=service.paths.repo("demo"), ops=None)

    def _fake_create(
        _ctx: object, tb: TrackedBranch, *_args: object, **_kwargs: object
    ) -> gh.PullRequest:
        pr_order.append(tb.branch)
        return gh.PullRequest(
            number=len(pr_order),
            url=f"https://gh/fake/pull/{len(pr_order)}",
            title="fake",
            body="",
            head_ref_name=tb.branch,
            base_ref_name="main",
            state="OPEN",
            is_draft=False,
        )

    # push_ops uses namespace imports (`worktree.run_single_pp`) so patching the
    # module attribute takes effect. Same for pr_create_update.*.
    monkeypatch.setattr(push_ops.worktree, "run_single_pp", _fake_pp)
    monkeypatch.setattr(push_ops.worktree, "acquired_for_op", _fake_acquired_for_op)
    monkeypatch.setattr(pr_create_update, "create_or_update_current_pr", _fake_create)
    monkeypatch.setattr(
        push_ops.pr_create_update, "refresh_component_pr_bodies", lambda *_a, **_k: None
    )

    service.push(SelectorTarget(repo_name="demo", branch="feature-c"), PushOptions(publish=True))
    assert pp_order == ["feature-a", "feature-b", "feature-c"]
    assert pr_order == ["feature-a", "feature-b", "feature-c"]
