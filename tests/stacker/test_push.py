"""Coverage for the scope-aware `push` command (force-push + PR refresh)."""
from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.stacker import git as stacker_git
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import (
    PushOptions,
    ScopeSpec,
    SelectorTarget,
)
from project_manager.stacker.service import StackerService

from .conftest import TrackedStack
from .fakes import RecordingPRBackend


def _configure_upstreams(stack: TrackedStack) -> None:
    for branch, slot in stack.slots.items():
        stacker_git.git(slot.path, "remote", "add", "origin-fake",
                        "git@github.com:acme/widgets.git", check=False)
        stacker_git.git(slot.path, "config", f"branch.{branch}.remote", "origin-fake")
        stacker_git.git(slot.path, "config", f"branch.{branch}.merge",
                        f"refs/heads/{branch}")


@pytest.fixture
def backend() -> RecordingPRBackend:
    return RecordingPRBackend()


@pytest.fixture
def push_service(
    pm_env: Paths,
    backend: RecordingPRBackend,
    tracked_stack: TrackedStack,
    monkeypatch: pytest.MonkeyPatch,
) -> StackerService:
    svc = StackerService(StackerDB(pm_env.stacker_db()), pm_env, pr_backend=backend)
    svc.db.set_config(tracked_stack.repo_name, "pr.mode", "repo-pr")
    svc.db.set_config(tracked_stack.repo_name, "pr.trunk", "main")
    _configure_upstreams(tracked_stack)
    # `git pp --force` is a Databricks alias; stub out the push itself.
    monkeypatch.setattr(svc, "_run_single_pp", lambda *_a, **_kw: True)
    return svc


def test_push_default_scope_walks_lineage(
    push_service: StackerService,
    backend: RecordingPRBackend,
    tracked_stack: TrackedStack,
) -> None:
    """Default `push` from `b` creates/updates PRs for ancestors + self + descendants."""
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    push_service.push(target)
    heads = [req.head for req, _body in backend.created]
    assert heads == ["a", "b", "c", "d"]


def test_push_only_limits_to_target(
    push_service: StackerService,
    backend: RecordingPRBackend,
    tracked_stack: TrackedStack,
) -> None:
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    push_service.push(target, PushOptions(scope=ScopeSpec(only=True)))
    heads = [req.head for req, _body in backend.created]
    assert heads == ["b"]


def test_push_skip_ancestors_drops_roots(
    push_service: StackerService,
    backend: RecordingPRBackend,
    tracked_stack: TrackedStack,
) -> None:
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    push_service.push(
        target, PushOptions(scope=ScopeSpec(skip_ancestors=True)),
    )
    heads = [req.head for req, _body in backend.created]
    assert heads == ["b", "c", "d"]


def test_push_skip_descendants_keeps_only_upper(
    push_service: StackerService,
    backend: RecordingPRBackend,
    tracked_stack: TrackedStack,
) -> None:
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    push_service.push(
        target, PushOptions(scope=ScopeSpec(skip_descendants=True)),
    )
    heads = [req.head for req, _body in backend.created]
    assert heads == ["a", "b"]


def test_push_draft_flag_forces_every_pr_draft(
    push_service: StackerService,
    backend: RecordingPRBackend,
    tracked_stack: TrackedStack,
) -> None:
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    push_service.push(target, PushOptions(draft=True))
    for req, _body in backend.created:
        assert req.draft is True


def test_push_publish_flag_forces_every_pr_published(
    push_service: StackerService,
    backend: RecordingPRBackend,
    tracked_stack: TrackedStack,
) -> None:
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    push_service.push(target, PushOptions(publish=True))
    for req, _body in backend.created:
        assert req.draft is False


def test_push_default_draft_policy_leaf_published(
    push_service: StackerService,
    backend: RecordingPRBackend,
    tracked_stack: TrackedStack,
) -> None:
    """Default: non-leaf entries draft, leaf (last in scope) published."""
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    push_service.push(target)
    draft_by_head = {req.head: req.draft for req, _body in backend.created}
    # Leaf under default current scope from b → lineage leaf = d.
    assert draft_by_head["d"] is False
    for non_leaf in ("a", "b", "c"):
        assert draft_by_head[non_leaf] is True


def test_push_create_pr_false_only_force_pushes(
    push_service: StackerService,
    backend: RecordingPRBackend,
    tracked_stack: TrackedStack,
) -> None:
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    result = push_service.push(target, PushOptions(create_pr=False))
    assert "Push complete." in result
    assert backend.created == []


def test_push_conflicting_draft_publish_errors(
    push_service: StackerService,
    tracked_stack: TrackedStack,
) -> None:
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    with pytest.raises(stacker_git.GitError, match="mutually exclusive"):
        push_service.push(target, PushOptions(draft=True, publish=True))


def test_push_empty_scope_when_target_has_no_lineage(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    backend: RecordingPRBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pushing from an untracked target in a fresh repo is a graceful no-op."""
    repo_name, _repo_path = stacker_repo
    svc = StackerService(StackerDB(pm_env.stacker_db()), pm_env, pr_backend=backend)
    monkeypatch.setattr(svc, "_run_single_pp", lambda *_a, **_kw: True)
    target = SelectorTarget(repo_name=repo_name, branch="ghost")
    with pytest.raises(stacker_git.GitError, match="not tracked"):
        svc.push(target)


