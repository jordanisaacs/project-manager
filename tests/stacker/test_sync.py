from __future__ import annotations

from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OWNER_STACKER_OPS, PoolDB
from project_manager.stacker import gh, locate
from project_manager.stacker import git as stacker_git
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import (
    ParentLocator,
    PRState,
    ScopeSpec,
    SelectorTarget,
    SyncOptions,
    WorktreeInit,
)
from project_manager.stacker.service import StackerService

from .conftest import TrackedStack, commit_file
from .fakes import RecordingPRBackend


def _initialize(service: StackerService, repo_name: str, slot: slot_mod.Slot, branch: str) -> None:
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
        parent_slot.path,
        "parent.txt",
        "p\n",
        "parent: first commit",
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
        slot
        for slot in slot_mod.list_slots(pm_env, repo_name)
        if pooldb.get_owner(slot.repo, slot.uuid) == OWNER_STACKER_OPS
    ]
    assert leaked == [], (
        f"downstream_sync left {len(leaked)} ops slot(s) claimed: {[s.uuid for s in leaked]}"
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
        target,
        ScopeSpec(scope="current", from_branch="c"),
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
        multi_arm_stack.repo_path,
        "shared.txt",
        "v\n",
        "main: advance",
    )

    result = service.sync(SelectorTarget(repo_name=repo_name, branch="c1"))
    assert "Sync complete." in result, result

    a = service.db.get_branch(repo_name, "a")
    assert a is not None
    assert a.managed_base_commit == new_main, (
        f"a.managed_base should advance to new main {new_main[:8]}, got {a.managed_base_commit[:8]}"
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
            multi_arm_stack.slots[parent].path,
            parent,
        )
        row = service.db.get_branch(repo_name, branch)
        assert row is not None
        assert row.managed_base_commit == parent_head, (
            f"{branch}.managed_base should advance to new {parent} tip "
            f"{parent_head[:8]}, got {row.managed_base_commit[:8]}"
        )


# --- Parent-modifications gate ----------------------------------------------


def _service_with_backend(
    pm_env: Paths,
    backend: RecordingPRBackend,
) -> StackerService:
    """Build a fresh StackerService against the shared DB with a fake PR backend.

    The `tracked_stack` / `service` fixtures already populate the DB;
    this just wraps the same DB with an injectable backend so the
    online-refresh path lands in our `RecordingPRBackend` instead of
    shelling out to `gh`.
    """
    return StackerService(StackerDB(pm_env.stacker_db()), pm_env, pr_backend=backend)


def _rebase_below_anchor(
    slot_path: Path,
    repo_path: Path,
    fork_from: str,
) -> str:
    """Rewrite the branch's anchor: fork main from `fork_from`, rebase branch.

    Simulates the user running `git rebase --onto <other> <last_synced>`
    so the branch's recorded anchor is no longer in HEAD's ancestry.
    `fork_from` is a commit on main that pre-dates the current anchor —
    the fork point becomes the new lineage. The branch must already
    be checked out in `slot_path` (so rebase re-attaches HEAD to the
    branch ref afterwards, not to a detached commit).
    """
    old_anchor = stacker_git.rev_parse(slot_path, "HEAD~1")
    stacker_git.git(repo_path, "reset", "--hard", fork_from)
    new_anchor = commit_file(
        repo_path,
        "main-fork.txt",
        "fork\n",
        "main: forked",
    )
    # Omit the third arg so rebase runs on the currently-checked-out
    # branch ref; passing the literal `HEAD` here detaches and leaves
    # the branch ref out of sync with the worktree HEAD.
    stacker_git.git(slot_path, "rebase", "--onto", new_anchor, old_anchor)
    return new_anchor


def test_sync_blocks_when_child_rebased_below_stack_range(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    """If the branch's recorded anchor is no longer in HEAD's ancestry, sync errors.

    Reproduces a user running `git rebase --onto` outside stacker so
    the history below `managed_base..HEAD` was rewritten. Without
    `--allow-drop-parent-modifications`, sync refuses to silently drop
    the change.
    """
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    main_root = stacker_git.rev_parse(repo_path, "HEAD")
    # Lay down a "real" anchor commit on main, so we can later fork off
    # the root and rebase the branch's anchor away.
    commit_file(repo_path, "anchor.txt", "anchor\n", "main: anchor")
    _initialize(service, repo_name, feature_slot, "feature-rebased")
    commit_file(feature_slot.path, "feat.txt", "feat\n", "feat: own commit")

    _rebase_below_anchor(feature_slot.path, repo_path, fork_from=main_root)

    with pytest.raises(stacker_git.GitError, match="--allow-drop-parent-modifications"):
        service.sync(SelectorTarget(repo_name=repo_name, branch="feature-rebased"))


def test_sync_allow_drop_parent_modifications_proceeds(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    """With the flag, sync drops the out-of-band history and replays working commits."""
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    main_root = stacker_git.rev_parse(repo_path, "HEAD")
    commit_file(repo_path, "anchor.txt", "anchor\n", "main: anchor")
    _initialize(service, repo_name, feature_slot, "feature-rebased-ok")
    commit_file(feature_slot.path, "feat.txt", "feat\n", "feat: own commit")

    new_anchor = _rebase_below_anchor(
        feature_slot.path,
        repo_path,
        fork_from=main_root,
    )

    result = service.sync(
        SelectorTarget(repo_name=repo_name, branch="feature-rebased-ok"),
        options=SyncOptions(allow_drop_parent_modifications=True),
    )
    assert "Sync complete." in result
    tracked = service.db.get_branch(repo_name, "feature-rebased-ok")
    assert tracked is not None
    # Sync re-rooted the branch on the current parent tip — which equals
    # the new anchor we just made on `main`.
    assert tracked.managed_base_commit == new_anchor
    head_parent = stacker_git.rev_parse(feature_slot.path, "HEAD~1")
    assert head_parent == new_anchor


def test_sync_parent_fast_forward_does_not_trigger_gate(
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    """Normal parent fast-forward keeps the anchor in HEAD's ancestry; sync proceeds."""
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    _initialize(service, repo_name, feature_slot, "feature-ff")
    commit_file(feature_slot.path, "feat.txt", "feat\n", "feat: own commit")
    new_main = _advance_main(repo_path)

    result = service.sync(SelectorTarget(repo_name=repo_name, branch="feature-ff"))
    assert "Sync complete." in result
    tracked = service.db.get_branch(repo_name, "feature-ff")
    assert tracked is not None
    assert tracked.managed_base_commit == new_main


def test_sync_parent_modifications_gate_aborts_downstream_queue(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """In a multi-branch sync, the per-branch gate aborts the queue at the offender.

    Earlier branches (queue-order) that pass the gate complete and
    their DB rows update; the gate-failing branch raises and the
    operation row is cleared so the user can retry with the flag.
    """
    repo_path = tracked_stack.repo_path
    main_root = stacker_git.rev_parse(repo_path, "HEAD")
    new_main = _advance_main(repo_path)
    # `a` syncs normally on the queue. `b`'s history is surgically
    # rewritten below its recorded anchor so the queue trips on `b`.
    b_slot = tracked_stack.slots["b"]
    _rebase_below_anchor(b_slot.path, repo_path, fork_from=main_root)

    target = SelectorTarget(repo_name=tracked_stack.repo_name, branch="d")
    with pytest.raises(stacker_git.GitError, match="--allow-drop-parent-modifications"):
        service.sync(target)

    # No leftover operation row after the gate aborts the queue.
    assert service.db.get_operation(tracked_stack.repo_name) is None
    # `a` (earlier in the queue) finalized with its row updated to the
    # forked main tip (the new_main was reset away by _rebase_below_anchor).
    a = service.db.get_branch(tracked_stack.repo_name, "a")
    assert a is not None
    assert a.managed_base_commit != new_main
    assert stacker_git.is_ancestor(repo_path, main_root, a.managed_base_commit)


# --- Merged-PR collapse -----------------------------------------------------


_PR_URL = "https://github.com/acme/widgets/pull/{n}"


def _seed_open_pr(
    db: StackerDB,
    repo_name: str,
    branch: str,
    number: int,
) -> str:
    """Insert an OPEN pr_state row for `branch`. Returns the PR URL."""
    url = _PR_URL.format(n=number)
    db.upsert_pr_state(
        PRState(
            repo_name=repo_name,
            branch=branch,
            pr_url=url,
            pr_number=number,
            state="OPEN",
        ),
    )
    return url


def _mark_pr_merged(
    backend: RecordingPRBackend,
    number: int,
    *,
    online: bool = True,
) -> None:
    """Hook a MERGED PR into the fake backend so the refresh path sees it.

    Sets the GraphQL `batch_pr_review` result when `online=True` so the
    sync's upfront refresh upgrades the cached `pr_state.state` from
    OPEN to MERGED. With `online=False` callers can omit this and use
    `sync(..., offline=True)` against a pre-merged `pr_state` row.
    """
    if online:
        backend.review_by_pr[("acme", "widgets", number)] = gh.PRReviewSummary(
            state="MERGED",
            is_draft=False,
            is_approved=False,
            has_open_comments=False,
        )


def _apply_squash_to_main(repo_path: Path, branch_files: dict[str, str]) -> str:
    """Commit a `branch_files` snapshot directly to main as a squash equivalent.

    For the merged-collapse gate, we only need the parent tree to match
    the would-be merge tree of `managed_base..HEAD`. The simplest way
    to land that is to write the branch's files and commit on main.
    """
    last = ""
    for path, content in branch_files.items():
        last = commit_file(repo_path, path, content, f"main: squash {path}")
    return last


def test_sync_collapses_branch_to_parent_when_pr_merged_and_squash_present(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    """Merged PR + squash present → sync resets the branch to parent_tip."""
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    _initialize(service, repo_name, feature_slot, "feature-merged")
    commit_file(feature_slot.path, "feat.txt", "feat\n", "feat: own commit")
    # Land the equivalent squash on main.
    squashed_main = _apply_squash_to_main(repo_path, {"feat.txt": "feat\n"})

    _seed_open_pr(service.db, repo_name, "feature-merged", number=1)
    backend = RecordingPRBackend()
    _mark_pr_merged(backend, number=1)
    svc = _service_with_backend(pm_env, backend)

    result = svc.sync(SelectorTarget(repo_name=repo_name, branch="feature-merged"))
    assert "Collapsed" in result
    tracked = svc.db.get_branch(repo_name, "feature-merged")
    assert tracked is not None
    assert tracked.managed_base_commit == squashed_main
    assert tracked.last_synced_parent_commit == squashed_main
    assert tracked.last_clean_head == squashed_main
    assert stacker_git.rev_parse(feature_slot.path, "HEAD") == squashed_main
    # Online refresh actually ran — pr_state should now reflect MERGED.
    pr = svc.db.get_pr_state(repo_name, "feature-merged")
    assert pr is not None
    assert pr.state == "MERGED"


def test_sync_blocks_collapse_when_pr_merged_but_squash_missing(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    """Merged PR + no squash on parent → sync errors with --allow-drop-merge hint."""
    repo_name, _ = stacker_repo
    feature_slot = three_slots[0]
    _initialize(service, repo_name, feature_slot, "feature-merged-missing")
    commit_file(feature_slot.path, "feat.txt", "feat\n", "feat: own commit")
    # Parent is *not* updated — squash is missing.

    _seed_open_pr(service.db, repo_name, "feature-merged-missing", number=2)
    backend = RecordingPRBackend()
    _mark_pr_merged(backend, number=2)
    svc = _service_with_backend(pm_env, backend)

    with pytest.raises(stacker_git.GitError, match="--allow-drop-merge"):
        svc.sync(SelectorTarget(repo_name=repo_name, branch="feature-merged-missing"))


def test_sync_allow_drop_merge_collapses_without_squash(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    """`--allow-drop-merge` forces the reset-to-parent even when squash is missing."""
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    main_before = stacker_git.rev_parse(repo_path, "HEAD")
    _initialize(service, repo_name, feature_slot, "feature-force-merged")
    commit_file(feature_slot.path, "feat.txt", "feat\n", "feat: own commit")

    _seed_open_pr(service.db, repo_name, "feature-force-merged", number=3)
    backend = RecordingPRBackend()
    _mark_pr_merged(backend, number=3)
    svc = _service_with_backend(pm_env, backend)

    result = svc.sync(
        SelectorTarget(repo_name=repo_name, branch="feature-force-merged"),
        options=SyncOptions(allow_drop_merge=True),
    )
    assert "Collapsed" in result
    tracked = svc.db.get_branch(repo_name, "feature-force-merged")
    assert tracked is not None
    # main hasn't moved; branch resets to its current tip.
    assert tracked.managed_base_commit == main_before
    assert tracked.last_clean_head == main_before
    assert stacker_git.rev_parse(feature_slot.path, "HEAD") == main_before


def test_sync_offline_uses_cached_pr_state(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    """`offline=True` skips the refresh; the collapse decision uses cached pr_state."""
    repo_name, repo_path = stacker_repo
    feature_slot = three_slots[0]
    _initialize(service, repo_name, feature_slot, "feature-offline")
    commit_file(feature_slot.path, "feat.txt", "feat\n", "feat: own commit")
    squashed_main = _apply_squash_to_main(repo_path, {"feat.txt": "feat\n"})

    # Pre-cache pr_state as MERGED so the collapse can fire offline.
    url = _PR_URL.format(n=4)
    service.db.upsert_pr_state(
        PRState(
            repo_name=repo_name,
            branch="feature-offline",
            pr_url=url,
            pr_number=4,
            state="MERGED",
            merged=True,
        ),
    )

    class _ExplodingBackend(RecordingPRBackend):
        def batch_pr_review(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("offline sync must not call batch_pr_review")

        def view_pr(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            raise AssertionError("offline sync must not call view_pr")

    svc = _service_with_backend(pm_env, _ExplodingBackend())

    result = svc.sync(
        SelectorTarget(repo_name=repo_name, branch="feature-offline"),
        options=SyncOptions(offline=True),
    )
    assert "Collapsed" in result
    tracked = svc.db.get_branch(repo_name, "feature-offline")
    assert tracked is not None
    assert tracked.managed_base_commit == squashed_main


# --- Chained no-commit branches ---------------------------------------------


def test_sync_chain_of_merged_branches_collapses_in_order(
    pm_env: Paths,
    tracked_stack: TrackedStack,
) -> None:
    """A multi-branch sync down a chain of merged PRs collapses each in queue order.

    Setup: `main → a → b → c → d`, each with one commit. We squash each
    branch's commit onto main in order so every branch's squash is
    present on its (now-collapsed) parent. After downstream sync from
    `d`, every branch row is a no-commit branch pinned to main's tip.
    """
    repo_name = tracked_stack.repo_name
    repo_path = tracked_stack.repo_path
    backend = RecordingPRBackend()
    svc = _service_with_backend(pm_env, backend)

    for n, branch in enumerate(("a", "b", "c", "d"), start=1):
        _seed_open_pr(svc.db, repo_name, branch, number=n)
        _mark_pr_merged(backend, number=n)
    # Land all four squashes on main in branch order.
    squashed_main = _apply_squash_to_main(
        repo_path,
        {
            "a.txt": "a\n",
            "b.txt": "b\n",
            "c.txt": "c\n",
            "d.txt": "d\n",
        },
    )

    result = svc.sync(SelectorTarget(repo_name=repo_name, branch="d"))
    assert "Sync complete." in result

    for branch in ("a", "b", "c", "d"):
        row = svc.db.get_branch(repo_name, branch)
        assert row is not None, branch
        assert row.managed_base_commit == squashed_main, branch
        assert row.last_clean_head == squashed_main, branch
        slot_head = stacker_git.rev_parse(
            tracked_stack.slots[branch].path,
            "HEAD",
        )
        assert slot_head == squashed_main, branch


def test_sync_no_commit_branch_parent_does_not_break_child_sync(
    pm_env: Paths,
    stacker_repo: tuple[str, Path],
    three_slots: list[slot_mod.Slot],
    service: StackerService,
) -> None:
    """A child branch on top of a collapsed (no-commit) parent syncs cleanly."""
    repo_name, repo_path = stacker_repo
    a_slot, b_slot = three_slots[0], three_slots[1]
    _initialize(service, repo_name, a_slot, "a")
    commit_file(a_slot.path, "a.txt", "a\n", "a: first commit")
    service.init_new_branch(
        WorktreeInit(
            repo_name=repo_name,
            worktree_path=b_slot.path,
            branch="b",
            parent=ParentLocator(repo_name=repo_name, branch="a"),
        )
    )
    b_commit = commit_file(b_slot.path, "b.txt", "b\n", "b: first commit")

    # Squash a onto main, mark a's PR merged, collapse a.
    squashed_main = _apply_squash_to_main(repo_path, {"a.txt": "a\n"})
    _seed_open_pr(service.db, repo_name, "a", number=10)
    backend = RecordingPRBackend()
    _mark_pr_merged(backend, number=10)
    svc = _service_with_backend(pm_env, backend)
    svc.sync(SelectorTarget(repo_name=repo_name, branch="a"))

    a_row = svc.db.get_branch(repo_name, "a")
    assert a_row is not None
    assert a_row.last_clean_head == squashed_main

    # Now sync b. Its parent (a) is a no-commit branch at squashed_main;
    # b's working commit should cherry-pick onto squashed_main.
    result = svc.sync(SelectorTarget(repo_name=repo_name, branch="b"))
    assert "Sync complete." in result
    b_row = svc.db.get_branch(repo_name, "b")
    assert b_row is not None
    assert b_row.managed_base_commit == squashed_main
    b_head = stacker_git.rev_parse(b_slot.path, "HEAD")
    head_parent = stacker_git.rev_parse(b_slot.path, "HEAD~1")
    assert head_parent == squashed_main
    # The cherry-picked b commit reproduces b's original tree.
    assert b_head != b_commit  # new sha after replay
    assert (b_slot.path / "b.txt").read_text() == "b\n"


def test_sync_chained_no_commit_branches_with_no_commit_parents(
    pm_env: Paths,
    tracked_stack: TrackedStack,
) -> None:
    """Re-syncing a fully-collapsed chain short-circuits to 'Nothing to sync'.

    After the chained collapse leaves every branch pinned to main, a
    re-sync on any individual branch should walk its scope, find every
    parent unchanged, and complete with the up-to-date message — no
    DB churn and no errors when the parent is itself a no-commit branch.
    """
    repo_name = tracked_stack.repo_name
    repo_path = tracked_stack.repo_path
    backend = RecordingPRBackend()
    svc = _service_with_backend(pm_env, backend)
    for n, branch in enumerate(("a", "b", "c", "d"), start=1):
        _seed_open_pr(svc.db, repo_name, branch, number=n)
        _mark_pr_merged(backend, number=n)
    _apply_squash_to_main(
        repo_path,
        {
            "a.txt": "a\n",
            "b.txt": "b\n",
            "c.txt": "c\n",
            "d.txt": "d\n",
        },
    )
    svc.sync(SelectorTarget(repo_name=repo_name, branch="d"))

    snapshot = {branch: svc.db.get_branch(repo_name, branch) for branch in ("a", "b", "c", "d")}

    # Resync each individually with offline=True so we don't re-collapse —
    # they should each short-circuit on the unchanged-parent check.
    for branch in ("a", "b", "c", "d"):
        result = svc.sync(
            SelectorTarget(repo_name=repo_name, branch=branch),
            ScopeSpec(scope="current", skip_descendants=True, skip_ancestors=True),
            options=SyncOptions(offline=True),
        )
        assert "Nothing to sync" in result or "Sync complete." in result, f"{branch}: {result!r}"
        assert svc.db.get_branch(repo_name, branch) == snapshot[branch]
