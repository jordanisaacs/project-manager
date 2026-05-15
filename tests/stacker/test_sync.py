from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OWNER_STACKER_OPS, PoolDB
from project_manager.stacker import git as stacker_git
from project_manager.stacker import locate
from project_manager.stacker.models import (
    ParentLocator,
    ScopeSpec,
    SelectorTarget,
    WorktreeInit,
)
from project_manager.stacker.service import StackerService

from .conftest import TrackedStack, commit_file


def _initialize(
    service: StackerService, repo_name: str, slot: slot_mod.Slot, branch: str
) -> None:
    service.init_new_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=slot.path,
            branch=branch,
            parent=ParentLocator(repo_name=repo_name, branch="main"),
        )
    )


def _advance_main(repo_path: Path) -> str:
    """Commit directly to main in the pm 'repo' (which has main checked out)."""
    return commit_file(repo_path, "extra.txt", "extra\n", "extra")


def test_sync_against_live_worktree(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    _initialize(service, repo_name, feature_slot, "feature-a")
    commit_file(feature_slot.path, "feat.txt", "work\n", "feature work")

    new_main = _advance_main(repo_path)
    assert new_main != ""

    result = service.sync(SelectorTarget(repo_name=repo_name, branch="feature-a"))
    assert "Sync complete." in result
    tracked = service.db.get_branch(repo_name, "feature-a")
    assert tracked is not None
    assert tracked.managed_base_commit == new_main


def test_sync_branch_not_checked_out_acquires_ops_slot(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    _initialize(service, repo_name, feature_slot, "feature-b")
    commit_file(feature_slot.path, "feat.txt", "work\n", "feature work")
    # Simulate the branch moving out of any pool slot: detach the slot.
    stacker_git.git(feature_slot.path, "checkout", "--detach", "HEAD")
    pooldb = PoolDB(pm_env.pool_db())
    if not pooldb.is_free(feature_slot.repo, feature_slot.uuid):
        pooldb.release(feature_slot.repo, feature_slot.uuid)
    assert locate.locate_worktree(pm_env, repo_name, "feature-b") is None

    _advance_main(repo_path)

    result = service.sync(SelectorTarget(repo_name=repo_name, branch="feature-b"))
    assert "Sync complete." in result
    # After clean completion no slot should be held by the ops marker.
    for slot in slot_mod.list_slots(pm_env, repo_name):
        owner = pooldb.get_owner(slot.repo, slot.uuid)
        assert owner != OWNER_STACKER_OPS


def test_sync_multi_branch_releases_leaf_slot(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    """Regression: `downstream_sync` must release every slot it acquires,
    including the last branch in the queue. The driver's per-iteration
    transition (`finalize_local_op` returns None → loop continues to
    next branch) used to drop `handle` without releasing — leaving the
    final branch's slot stuck as `stacker|ops` after a clean sync.
    """
    repo_name, repo_path = stacker_repo
    parent_slot = three_slots[0]
    leaf_slot = three_slots[1]
    _initialize(service, repo_name, parent_slot, "feature-parent")
    parent_commit = commit_file(
        parent_slot.path, "parent.txt", "p\n", "parent: first commit",
    )
    service.init_new_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=leaf_slot.path,
            branch="feature-leaf",
            parent=ParentLocator(repo_name=repo_name, branch="feature-parent"),
        )
    )
    commit_file(leaf_slot.path, "leaf.txt", "l\n", "leaf: first commit")
    # Detach the leaf so sync has to mint an ops slot for it — that's the
    # path that exposed the leak.
    stacker_git.git(leaf_slot.path, "checkout", "--detach", "HEAD")
    pooldb = PoolDB(pm_env.pool_db())
    if not pooldb.is_free(leaf_slot.repo, leaf_slot.uuid):
        pooldb.release(leaf_slot.repo, leaf_slot.uuid)

    # Advance main so the parent's sync has work to do; the leaf rides
    # along behind and gets its own cherry-pick onto the new parent tip.
    _advance_main(repo_path)
    assert parent_commit != ""

    result = service.sync(SelectorTarget(repo_name=repo_name, branch="feature-leaf"))
    assert "Sync complete." in result

    leaked = [
        slot for slot in slot_mod.list_slots(pm_env, repo_name)
        if pooldb.get_owner(slot.repo, slot.uuid) == OWNER_STACKER_OPS
    ]
    assert leaked == [], (
        f"downstream_sync left {len(leaked)} ops slot(s) claimed: "
        f"{[s.uuid for s in leaked]}"
    )


def test_sync_paused_on_conflict_keeps_slot_claimed(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    _initialize(service, repo_name, feature_slot, "feature-c")
    commit_file(feature_slot.path, "shared.txt", "feature version\n", "feature touches shared")

    # Advance main with a conflicting change on shared.txt.
    commit_file(repo_path, "shared.txt", "main version\n", "main touches shared")

    result = service.sync(SelectorTarget(repo_name=repo_name, branch="feature-c"))
    assert "paused" in result.lower()
    op = service.db.get_operation(repo_name)
    assert op is not None
    assert op.status == "paused"

    # Abort cleans up and the slot returns to pool.
    abort_result = service.abort_operation(repo_name)
    assert "Aborted" in abort_result
    assert service.db.get_operation(repo_name) is None


# --- Scope-flag coverage ----------------------------------------------------


def test_sync_default_scope_walks_lineage(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """Default scope `-c` walks ancestors + target + descendants.

    With `main` unchanged, every branch is up-to-date; the walk still
    visits each and reports "Sync complete." once.
    """
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    result = service.sync(target)
    assert "Sync complete." in result


def test_sync_skip_ancestors_matches_old_push_behavior(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`--skip-ancestors` syncs target + descendants (the old `push` meaning).

    Add a commit to `b`, expect `b`'s tip becomes the managed base of
    descendants `c` and `d` after sync walk.
    """
    b_slot = tracked_stack.slots["b"]
    new_b_head = commit_file(b_slot.path, "b-extra.txt", "b+\n", "b: extra")
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")

    result = service.sync(target, ScopeSpec(scope="current", skip_ancestors=True))
    assert "Sync complete." in result

    c_tracked = service.db.get_branch(tracked_stack.repo_name, "c")
    d_tracked = service.db.get_branch(tracked_stack.repo_name, "d")
    assert c_tracked is not None
    assert d_tracked is not None
    assert c_tracked.managed_base_commit == new_b_head
    assert d_tracked.last_synced_parent_commit is not None


def test_sync_skip_descendants_leaves_descendants_untouched(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`--skip-descendants` must not rewrite descendants' managed bases."""
    before_d = service.db.get_branch(tracked_stack.repo_name, "d")
    assert before_d is not None

    # Advance main so ancestors need a sync — but descendants should be skipped.
    commit_file(tracked_stack.repo_path, "shared.txt", "v\n", "main advance")

    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    service.sync(target, ScopeSpec(scope="current", skip_descendants=True))

    after_d = service.db.get_branch(tracked_stack.repo_name, "d")
    assert after_d is not None
    assert after_d.managed_base_commit == before_d.managed_base_commit


def test_sync_all_walks_every_tracked_branch(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`--all` resolves to every tracked branch, regardless of target."""
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="a")
    result = service.sync(target, ScopeSpec(scope="all"))
    assert "Sync complete." in result


def test_sync_from_branch_trims_walk(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`--from c` on a lineage walk starts the walk at `c`."""
    commit_file(tracked_stack.repo_path, "main-bump.txt", "m\n", "main bump")
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    result = service.sync(
        target, ScopeSpec(scope="current", from_branch="c"),
    )
    assert "Sync complete." in result


def test_sync_from_branch_rejects_out_of_scope(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`--from nonexistent` raises rather than silently walking nothing."""
    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="b")
    with pytest.raises(stacker_git.GitError, match="--from"):
        service.sync(target, ScopeSpec(scope="current", from_branch="nonexistent"))


def test_sync_default_scope_walks_full_multi_arm_component(
    multi_arm_stack: TrackedStack,
    service: StackerService,
) -> None:
    """Default-scope sync from one arm must walk every branch in the component.

    Tree: `main → a → {b1 → c1; b2 → c2}`. Advancing main forces every
    branch to need a cherry-pick onto its parent's new tip. Sync from
    `c1` must cascade through the whole component so b2 and c2 also
    catch up — currently they're left behind because `lineage(c1)`
    skips sibling arms.
    """
    repo_name = multi_arm_stack.repo_name
    new_main = commit_file(
        multi_arm_stack.repo_path, "shared.txt", "v\n", "main: advance",
    )

    result = service.sync(SelectorTarget(repo_name=repo_name, branch="c1"))
    assert "Sync complete." in result, result

    a = service.db.get_branch(repo_name, "a")
    assert a is not None
    assert a.managed_base_commit == new_main, (
        f"a.managed_base should advance to new main {new_main[:8]}, "
        f"got {a.managed_base_commit[:8]}"
    )
    a_head = stacker_git.rev_parse(multi_arm_stack.slots["a"].path, "a")

    for branch, parent_head in (
        ("b1", a_head),
        ("b2", a_head),
    ):
        row = service.db.get_branch(repo_name, branch)
        assert row is not None
        assert row.managed_base_commit == parent_head, (
            f"{branch}.managed_base should advance to new {row.parent_branch} tip "
            f"{parent_head[:8]}, got {row.managed_base_commit[:8]} — "
            f"sibling arm was left out of the default-scope sync"
        )

    for branch, parent in (("c1", "b1"), ("c2", "b2")):
        parent_head = stacker_git.rev_parse(
            multi_arm_stack.slots[parent].path, parent,
        )
        row = service.db.get_branch(repo_name, branch)
        assert row is not None
        assert row.managed_base_commit == parent_head, (
            f"{branch}.managed_base should advance to new {parent} tip "
            f"{parent_head[:8]}, got {row.managed_base_commit[:8]}"
        )


# --- --hard flag and parent-rewrite detection -------------------------------


def _rewrite_main(repo_path: Path, before: str, content: str, message: str) -> str:
    """Reset main to `before` and lay down a fresh commit. Returns its sha.

    Simulates a non-fast-forward parent rewrite: the previous tip is no
    longer reachable from main.
    """
    stacker_git.git(repo_path, "reset", "--hard", before)
    return commit_file(repo_path, "shared.txt", content, message)


def test_sync_hard_replays_after_rewrite(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    """--hard replays exactly the child's own commits after a parent rewrite."""
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    main_before = stacker_git.rev_parse(repo_path, "HEAD")
    commit_file(repo_path, "shared.txt", "main v1\n", "main: v1")
    _initialize(service, repo_name, feature_slot, "feature-hard")
    commit_file(feature_slot.path, "feat.txt", "feat\n", "feat: own commit")

    service.sync(SelectorTarget(repo_name=repo_name, branch="feature-hard"))

    # Rewrite main with a non-conflicting change so the cherry-pick of the
    # child's own commit applies cleanly.
    main_v1_prime = _rewrite_main(repo_path, main_before, "main prime\n", "main: v1'")

    result = service.sync(
        SelectorTarget(repo_name=repo_name, branch="feature-hard"), hard=True,
    )
    assert "Sync complete." in result
    tracked = service.db.get_branch(repo_name, "feature-hard")
    assert tracked is not None
    assert tracked.managed_base_commit == main_v1_prime
    assert tracked.last_clean_head is not None
    # Branch HEAD should be `main_v1_prime + feat commit` — verify by walking
    # one parent back from the head.
    head_parent = stacker_git.rev_parse(feature_slot.path, "HEAD~1")
    assert head_parent == main_v1_prime


def test_sync_hard_equivalent_to_default_on_clean_advance(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    """When the parent is fast-forwarded, --hard and default produce the same outcome."""
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    _initialize(service, repo_name, feature_slot, "feature-eq")
    commit_file(feature_slot.path, "feat.txt", "feat\n", "feat: own commit")
    new_main = _advance_main(repo_path)

    result = service.sync(
        SelectorTarget(repo_name=repo_name, branch="feature-eq"), hard=True,
    )
    assert "Sync complete." in result
    tracked = service.db.get_branch(repo_name, "feature-eq")
    assert tracked is not None
    assert tracked.managed_base_commit == new_main


def test_sync_hard_continue_persists(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    """A paused --hard sync resumes via plain `continue` — flag survives in the DB."""
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    main_before = stacker_git.rev_parse(repo_path, "HEAD")
    commit_file(repo_path, "shared.txt", "main v1\n", "main: v1")
    _initialize(service, repo_name, feature_slot, "feature-pause")
    # Child touches shared.txt — cherry-pick onto rewritten main will conflict.
    commit_file(feature_slot.path, "shared.txt", "child v1\n", "feat: shared")

    service.sync(SelectorTarget(repo_name=repo_name, branch="feature-pause"))

    # Rewrite main with a conflicting change on shared.txt.
    _rewrite_main(repo_path, main_before, "main prime\n", "main: v1'")

    paused = service.sync(
        SelectorTarget(repo_name=repo_name, branch="feature-pause"), hard=True,
    )
    assert "paused" in paused.lower()
    op = service.db.get_operation(repo_name)
    assert op is not None
    assert op.hard is True

    # Resolve by taking the child's version.
    (feature_slot.path / "shared.txt").write_text("child v1\n")
    stacker_git.git(feature_slot.path, "add", "shared.txt")

    finished = service.continue_operation(repo_name)
    assert "Sync complete." in finished
    assert service.db.get_operation(repo_name) is None


def test_sync_pause_hints_at_hard_when_failing_commit_predates_working_set(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    """A non-hard sync pausing on an ancestor of managed_base suggests --hard.

    Setup mirrors the lbm-barnacle-config scenario: the parent has been
    rewritten in place, patch-id dedup misses the duplicate, and the
    cherry-pick replays a commit that is already in the child's recorded
    base. The failure message must point the user at `--hard`.
    """
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    main_before = stacker_git.rev_parse(repo_path, "HEAD")
    commit_file(repo_path, "shared.txt", "main v1\n", "main: v1")
    _initialize(service, repo_name, feature_slot, "feature-hint")
    commit_file(feature_slot.path, "feat.txt", "feat\n", "feat: own commit")

    service.sync(SelectorTarget(repo_name=repo_name, branch="feature-hint"))

    # Rewrite main with a conflicting change so the cherry-pick of the
    # main_v1 commit (an ancestor of managed_base) conflicts.
    _rewrite_main(repo_path, main_before, "main prime\n", "main: v1'")

    paused = service.sync(SelectorTarget(repo_name=repo_name, branch="feature-hint"))
    assert "paused" in paused.lower()
    assert "--hard" in paused
    assert "predates" in paused
    service.abort_operation(repo_name)


def test_sync_hard_pause_omits_hard_hint(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    """A pause from `sync --hard` must not suggest `--hard` again."""
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    main_before = stacker_git.rev_parse(repo_path, "HEAD")
    commit_file(repo_path, "shared.txt", "main v1\n", "main: v1")
    _initialize(service, repo_name, feature_slot, "feature-already-hard")
    commit_file(feature_slot.path, "shared.txt", "child v1\n", "feat: shared")

    service.sync(SelectorTarget(repo_name=repo_name, branch="feature-already-hard"))
    _rewrite_main(repo_path, main_before, "main prime\n", "main: v1'")

    paused = service.sync(
        SelectorTarget(repo_name=repo_name, branch="feature-already-hard"),
        hard=True,
    )
    assert "paused" in paused.lower()
    assert "--hard" not in paused
    service.abort_operation(repo_name)


def test_sync_hard_downstream_propagates(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """--hard applies to every branch in a multi-branch (downstream_sync) walk."""
    main_before = stacker_git.rev_parse(tracked_stack.repo_path, "HEAD")
    commit_file(tracked_stack.repo_path, "shared.txt", "main v1\n", "main: v1")

    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="d")
    service.sync(target)

    # Rewrite main non-conflictingly.
    new_main_prime = _rewrite_main(
        tracked_stack.repo_path, main_before, "main prime\n", "main: v1'",
    )

    result = service.sync(target, hard=True)
    assert "Sync complete." in result

    # All branches should be re-rooted on the rewritten main, which means
    # `a`'s managed_base now equals new_main_prime.
    a = service.db.get_branch(tracked_stack.repo_name, "a")
    assert a is not None
    assert a.managed_base_commit == new_main_prime
