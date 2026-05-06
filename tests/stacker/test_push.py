"""Coverage for the scope-aware `push` command (force-push + PR refresh)."""
from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OwnerKind, PoolDB
from project_manager.stacker import gh
from project_manager.stacker import git as stacker_git
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import (
    ParentLocator,
    PRState,
    PushOptions,
    ScopeSpec,
    SelectorTarget,
    WorktreeInit,
)
from project_manager.stacker.ops import worktree as ops_worktree
from project_manager.stacker.service import StackerService

from .conftest import TrackedStack, commit_file
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
    # `git pp --force` is a user push alias; stub out the push itself.
    monkeypatch.setattr(ops_worktree, "run_single_pp", lambda *_a, **_kw: True)
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


def _setup_fake_upstream(slot_path: Path, branch: str) -> None:
    stacker_git.git(slot_path, "remote", "add", "origin-fake",
                    "git@github.com:acme/widgets.git", check=False)
    stacker_git.git(slot_path, "config", f"branch.{branch}.remote", "origin-fake")
    stacker_git.git(slot_path, "config", f"branch.{branch}.merge",
                    f"refs/heads/{branch}")


def _seed_unattached_branch(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    init_slot: slot_mod.Slot,
    backend: RecordingPRBackend,
    branch: str,
) -> StackerService:
    """Track `branch` off `main` in `init_slot`, then detach so the slot is
    free of branch-bindings — `locate_worktree` returns None for `branch`.

    Mirrors the production "user committed and ran pp from a different
    cwd" path: the branch exists locally as a ref but no worktree has it
    checked out, so push must mint an ops slot to operate on it.
    """
    repo_name, _ = stacker_repo
    svc = StackerService(StackerDB(pm_env.stacker_db()), pm_env, pr_backend=backend)
    svc.db.set_config(repo_name, "pr.mode", "repo-pr")
    svc.db.set_config(repo_name, "pr.trunk", "main")
    svc.init_new_branch(WorktreeInit(
        repo_name=repo_name, worktree_path=init_slot.path, branch=branch,
        parent=ParentLocator(repo_name=repo_name, branch="main"),
    ))
    commit_file(init_slot.path, f"{branch}.txt", f"{branch}\n", f"{branch}: first commit")
    _setup_fake_upstream(init_slot.path, branch)
    stacker_git.git(init_slot.path, "checkout", "--detach", "HEAD")
    return svc


def test_push_releases_target_slot_after_clean_completion(
    pm_env: Paths,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: stacker-owned slots must not leak across pushes.

    Real workflow: `pm stacker push --branch X` minted an ops slot for X,
    held it on completion, and the next push of an unrelated branch
    waited indefinitely for that slot to free. The hold was unconditional
    — even when the worktree was clean, no work was in progress, and the
    user wasn't `cd`'d into it. Release stacker-owned slots after a
    successful push when there's nothing to preserve.
    """
    repo_name, _repo_path = stacker_repo
    monkeypatch.setattr(ops_worktree, "run_single_pp", lambda *_a, **_kw: True)
    svc = _seed_unattached_branch(pm_env, stacker_repo, three_slots[0], backend, "feature-x")

    pooldb = PoolDB(pm_env.pool_db())
    svc.push(SelectorTarget(repo_name=repo_name, branch="feature-x"), PushOptions(draft=True))

    stacker_owned = [
        uuid for repo, uuid, owner in pooldb.list_owned(repo_name)
        if owner.kind == OwnerKind.STACKER and repo == repo_name
    ]
    assert stacker_owned == [], (
        f"clean push left a stacker-owned slot leaked: {stacker_owned}"
    )


def test_push_holds_target_slot_when_worktree_dirty(
    pm_env: Paths,
    backend: RecordingPRBackend,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dirty worktrees retain their stacker slot — releasing would detach
    HEAD and silently throw away the user's uncommitted work.

    Tracked changes (modifications to committed files) are the trigger.
    `git status -uno` is the same predicate stacker uses elsewhere.
    """
    repo_name, _repo_path = stacker_repo
    monkeypatch.setattr(ops_worktree, "run_single_pp", lambda *_a, **_kw: True)
    svc = _seed_unattached_branch(pm_env, stacker_repo, three_slots[0], backend, "feature-y")

    # Inject dirtiness right before the slot-release decision: stub
    # `has_tracked_changes` so push sees the worktree as having unsaved
    # work. (Writing files into the slot path the test doesn't know in
    # advance is brittle; this is the cleanest way to control the
    # predicate stacker actually consults.)
    monkeypatch.setattr(stacker_git, "has_tracked_changes", lambda _path: True)

    pooldb = PoolDB(pm_env.pool_db())
    svc.push(SelectorTarget(repo_name=repo_name, branch="feature-y"), PushOptions(draft=True))

    stacker_owned = [
        uuid for repo, uuid, owner in pooldb.list_owned(repo_name)
        if owner.kind == OwnerKind.STACKER and repo == repo_name
    ]
    assert len(stacker_owned) == 1, (
        f"dirty worktree should keep the stacker slot held: {stacker_owned}"
    )


def _mark_merged(svc: StackerService, repo_name: str, branch: str, number: int) -> None:
    svc.db.upsert_pr_state(
        PRState(
            repo_name=repo_name,
            branch=branch,
            pr_url=f"https://github.com/acme/widgets/pull/{number}",
            pr_number=number,
            state="MERGED",
            merged=True,
        ),
    )


def test_push_skips_merged_ancestors(
    push_service: StackerService,
    backend: RecordingPRBackend,
    tracked_stack: TrackedStack,
) -> None:
    """Default-scope push from a leaf must not force-push merged ancestors.

    Reproduces the `pm stacker push` regression where a long-running
    branch sat downstream of an already-merged ancestor: every push from
    the leaf re-pushed the merged ref (clobbering the GitHub merge
    commit) and reopened body edits on the closed PR.
    """
    _mark_merged(push_service, tracked_stack.repo_name, "a", number=10)
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    result = push_service.push(target)
    heads = [req.head for req, _body in backend.created]
    assert "a" not in heads
    assert heads == ["b", "c", "d"]
    assert "Skipping demo:a" in result


def test_push_target_already_merged_short_circuits(
    push_service: StackerService,
    backend: RecordingPRBackend,
    tracked_stack: TrackedStack,
) -> None:
    """Pushing a merged target reports the merge and creates no PRs."""
    _mark_merged(push_service, tracked_stack.repo_name, "b", number=11)
    target = SelectorTarget(
        repo_name=tracked_stack.repo_name, branch="b",
    )
    result = push_service.push(
        target, PushOptions(scope=ScopeSpec(only=True)),
    )
    assert backend.created == []
    assert "already merged" in result


def test_push_empty_scope_when_target_has_no_lineage(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    backend: RecordingPRBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pushing from an untracked target in a fresh repo is a graceful no-op."""
    repo_name, _repo_path = stacker_repo
    svc = StackerService(StackerDB(pm_env.stacker_db()), pm_env, pr_backend=backend)
    monkeypatch.setattr(ops_worktree, "run_single_pp", lambda *_a, **_kw: True)
    target = SelectorTarget(repo_name=repo_name, branch="ghost")
    with pytest.raises(stacker_git.GitError, match="not tracked"):
        svc.push(target)


def _seed_existing_pr(  # noqa: PLR0913 (test helper — explicit args read better than a dataclass here)
    backend: RecordingPRBackend,
    push_service: StackerService,
    repo_name: str,
    branch: str,
    *,
    title: str,
    number: int = 42,
) -> gh.PullRequest:
    pr = gh.PullRequest(
        number=number,
        url=f"https://github.com/acme/widgets/pull/{number}",
        title=title,
        body="",
        head_ref_name=branch,
        base_ref_name="main",
        state="OPEN",
        is_draft=False,
    )
    backend.prs_by_head[("acme/widgets", branch)] = pr
    push_service.db.upsert_pr_state(
        PRState(
            repo_name=repo_name,
            branch=branch,
            pr_url=pr.url,
            pr_number=pr.number,
            state="OPEN",
        ),
    )
    return pr


def test_push_preserves_manual_title_edit(
    push_service: StackerService,
    backend: RecordingPRBackend,
    tracked_stack: TrackedStack,
) -> None:
    """A user-edited PR title must not be clobbered by subsequent pushes.

    Reproduces the bug where every `pm stacker push` reset the PR title
    to the bottom-commit subject, fighting any rewrite the user made via
    `gh pr edit --title` (or via the GitHub UI).
    """
    _seed_existing_pr(
        backend,
        push_service,
        tracked_stack.repo_name,
        "a",
        title="[cplane/api] feat: rewritten by hand",
    )
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="a")
    push_service.push(target, PushOptions(scope=ScopeSpec(only=True)))
    title_syncs = [
        req for req, _body in backend.edited if req.title is not None
    ]
    assert title_syncs == [], (
        f"manually-edited title should be preserved, got title_syncs={title_syncs}"
    )


def test_push_resyncs_title_when_existing_matches_bottom_subject(
    push_service: StackerService,
    backend: RecordingPRBackend,
    tracked_stack: TrackedStack,
) -> None:
    """Idempotent path: if the live title still matches the bottom-commit
    subject, pm passes the title through (a no-op for GitHub but a useful
    signal for tools layered on top of `EditPRRequest`)."""
    _seed_existing_pr(
        backend,
        push_service,
        tracked_stack.repo_name,
        "a",
        title="a: first commit",  # matches the tracked_stack commit subject
    )
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="a")
    push_service.push(target, PushOptions(scope=ScopeSpec(only=True)))
    title_syncs = [
        req.title for req, _body in backend.edited if req.title is not None
    ]
    assert title_syncs == ["a: first commit"]


