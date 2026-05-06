---
name: pm-stacker-workflow
description: |
  Cherry-pick-driven stacked PR workflow with `pm stacker`. Use when the user runs `pm stacker`,
  is on a `stack/*` branch in a pm-managed worktree, or asks about stacked PRs in this project.
  Triggers: "pm stacker", "stack/*", "stacked PR", "stacked branch", "pm stacker sync",
  "pm stacker push", "pm stacker create", "cherry-pick", "stack conflict"
---

# pm Stacker Workflow

Reference for managing stacked branches with `pm stacker`. Use when on `stack/*` branches in a pm-managed worktree, or when the user asks about stacked PRs with `pm`.

`pm stacker` is **cherry-pick driven**, not rebase driven. Commit SHAs change on every sync — reference branches by name, never SHA.

## Mental Model

- A **tracked branch** is a branch `pm stacker` knows about. It records a parent branch and the commit it was originally based on (the **managed base**).
- A **stack** is a chain of tracked branches: `master ← stack/a ← stack/b ← stack/c`.
- **Sync** means: cherry-pick this branch's commits onto the (possibly moved) parent. The branch's commits get **new SHAs** every sync — they are not stable.
- **Push** means: force-push the branch to its remote and create or update its GitHub PR.
- A paused **operation** (sync / reparent / absorb) holds the slot and is resumed with `pm stacker continue` or thrown away with `pm stacker abort`.

PRs are cached per branch in pm's database — `pm stacker ls` shows them without re-querying GitHub. `pm stacker pr refresh` re-syncs the cache.

## Critical Rules

1. **Always `pm stacker ls --json` before deciding what to do.** It returns each branch's `parent_branch`, `needs_sync`, `status`, `pr_url`, `merged`, `commit_count`, and `children` — everything needed to plan an action.
2. **NEVER `git pull` or `git rebase` on a `stack/*` branch.** The `pm-git-guard` zsh hook blocks them. Use `pm stacker sync` to incorporate parent changes.
3. **NEVER `git branch -D` a tracked branch.** Use `pm stacker remove` so child branches get reparented.
4. **NEVER `git push` / `git push --force` on a tracked branch.** Use `pm stacker push`.
5. **Always `pm stacker sync` before `pm stacker push`.** Push only sends what's already on the local branch; sync is what replays parent changes.
6. **Commit SHAs are not stable.** Cherry-pick produces new commits on every sync. Do not paste SHAs into PR descriptions, code comments, or anywhere durable. Reference branches by name.
7. **Default scope is the current lineage.** `pm stacker sync` and `pm stacker push` walk only the current branch and its ancestors/descendants by default. Use `--all` to operate on the whole repo's stacks.
8. **On conflict, the operation pauses.** Resolve in the worktree, `git add` the resolution, then `pm stacker continue`. Don't run `git cherry-pick --continue` directly — go through pm.
9. **`pm stacker create` mints a new branch and possibly claims a slot.** Don't run it unless the user asked for a new branch.

## Command Reference

### Inspecting state

| Command | Description | `--json` |
|---|---|---|
| `pm stacker ls` | Tree of tracked branches with PR / sync / merge status | ✓ |
| `pm stacker ls --details {none\|status\|status-counts\|all}` | Verbosity. Default is `status-counts`. | ✓ |
| `pm stacker ls -a / --all` | Show every tracked branch in the repo (default: current lineage only) | ✓ |
| `pm stacker ls --skip-ancestors` / `--skip-descendants` | Narrow the walk | ✓ |
| `pm stacker ls --from <branch>` | Anchor the walk at a specific branch | ✓ |
| `pm stacker log [--branch <b>]` | Commits since the branch's managed base |  |
| `pm stacker config [<key> [<value>]]` | Per-repo config (`pr.mode`, `pr.trunk`, `pr.target-repo`) | ✓ |

### Creating, naming, removing

| Command | Description |
|---|---|
| `pm stacker create --branch stack/<name> [--on <parent>\|--copy <src>\|--replace]` | New tracked branch. `--on` picks parent (default: current). `--copy <src>` copies from another branch. `--replace` adopts an existing branch into the stack. |
| `pm stacker rename <new-name>` | Rename current branch (also renames the PR-side ref where possible) |
| `pm stacker remove [--branch <b>] [--force] [--keep-branch] [--parent]` | Untrack and (by default) delete the branch; children are reparented up |
| `pm stacker split <new-name> <commit> [--stay]` | Move commits from `<commit>..HEAD` onto a new child branch |

### Daily flow

| Command | Description |
|---|---|
| `pm stacker sync [--branch <b>] [--all] [--from <b>] [--skip-ancestors\|--skip-descendants]` | Cherry-pick the branch (and lineage by default) onto its parent |
| `pm stacker sync --hard` | Cherry-pick exactly the commits added since last sync (`<managed_base>..HEAD`). Use when the parent was rewritten in place and patch-id dedup can't see the duplicate. |
| `pm stacker sync --continue` | Resume after resolving a cherry-pick conflict |
| `pm stacker sync --abort` | Roll back the in-progress sync |
| `pm stacker push [--branch <b>] [--all] [--only] [--draft\|--publish] [--create-pr {true\|false}]` | Force-push and create/update GitHub PRs. By default leaf=published, others=draft. |
| `pm stacker continue` | Resume any paused operation (sync / reparent / absorb) |
| `pm stacker abort` | Cancel any paused operation, return slot to clean state |

### Restructuring

| Command | Description |
|---|---|
| `pm stacker reparent <new-parent> [--branch <b>]` | Move branch onto a different parent; descendants are re-cherry-picked |
| `pm stacker reparent --continue / --abort` | Resume/cancel a paused reparent |
| `pm stacker absorb [--branch <b>]` | Cherry-pick the current branch's new commits onto its parent (one-way, child → parent) |
| `pm stacker absorb --continue / --abort` | Resume/cancel a paused absorb |

### PR cache

| Command | Description |
|---|---|
| `pm stacker pr refresh` | Look up the current branch's GitHub PR and cache it |
| `pm stacker pr unlink [--all]` | Clear the cached PR association (use after deleting a PR or rerouting) |

### Recovery / guard

| Command | Description |
|---|---|
| `pm stacker repair --base-ref <ref>` | Reset stored managed-base / last-synced / last-clean-head to match git (use after manual surgery) |
| `pm stacker guard no-rebase` | Internal — used by `pm-git-guard` to block `git pull` / `git rebase`. Don't invoke directly. |

## Stack Direction Reference

```
master                                    (untracked root)
  |
  +-- stack/feature-1-add-models          <-- "root" of this stack
        |                                   ^   towards master
        |
        +-- stack/feature-2-add-service
              |                             v   away from master
              |
              +-- stack/feature-3-add-api  <-- "leaf"
```

- `pm stacker ls` (default) shows the lineage of the current branch (its ancestors + descendants).
- `pm stacker sync` from `stack/feature-2-add-service` replays feature-2 onto its updated parent and then replays feature-3 on top of the freshly synced feature-2.
- `pm stacker push` from feature-3 pushes feature-1, feature-2, feature-3 (ancestors included by default) and creates/updates each PR.

## Daily Workflow

### Start a new stack

```bash
cd ~/.projects/my-proj/frontend       # land in a pm worktree
pm stacker ls --json                  # see what's already tracked
pm stacker create --branch stack/feature-a --on master
# ... edit code ...
git add <files> && git commit -m "[TICKET] Add feature A"
pm stacker push                       # creates the PR
```

### Add a child branch

```bash
# From stack/feature-a:
pm stacker create --branch stack/feature-b   # parent defaults to current branch
git add <files> && git commit -m "[TICKET] Add feature B"
pm stacker push                              # ancestors are included by default
```

### Update master and propagate

```bash
pm repo pull                          # fetch + ff master in the canonical clone
pm stacker sync                       # replay the lineage onto the new master
pm stacker push
```

`pm stacker sync` from a leaf branch handles the whole lineage. Don't run sync from `master` itself unless you really want every stack synced (`--all` is the explicit form).

### Edit an earlier branch

```bash
git checkout stack/feature-a          # jump to the branch that needs the fix
git add <files> && git commit         # or amend; both are fine, sync replays either
pm stacker sync                       # replays feature-b on top of the updated feature-a
git checkout stack/feature-b          # back to where you were
pm stacker push
```

## Conflict Resolution

When `pm stacker sync`, `reparent`, or `absorb` hits a cherry-pick conflict it pauses. The user sees a "paused" message and the worktree has standard cherry-pick markers (`CHERRY_PICK_HEAD`, conflict hunks).

```bash
# 1. Inspect what's stuck
pm stacker ls --json                  # current branch and its sync status
git status                            # shows conflicted files

# 2. Resolve conflicts in the editor

# 3. Stage the resolution
git add <resolved-files>

# 4. Continue
pm stacker continue                   # resumes the cherry-pick + remaining lineage
# or
pm stacker abort                      # rolls back to the pre-sync state
```

**Don't run `git cherry-pick --continue` directly** — `pm stacker continue` does that *and* advances the multi-branch queue (next child gets cherry-picked too).

If `continue` pauses again on the next branch, repeat. The slot stays held the whole time; the operation is durable across shells.

## Restructuring Workflows

### Reparent a branch onto a different parent

```bash
pm stacker reparent stack/new-parent
# or specify the branch:
pm stacker reparent stack/new-parent --branch stack/feature-c
```

If reparent conflicts: resolve, `git add`, `pm stacker continue`. To bail: `pm stacker abort`.

### Split commits off into a child branch

```bash
pm stacker split stack/feature-b <commit-sha>     # commits <sha>..HEAD move onto feature-b
# Original branch is reset to <sha>; feature-b is created as its child.
```

### Absorb new commits into the parent

```bash
# On stack/feature-b with new commits to fold into feature-a:
pm stacker absorb                                  # cherry-picks them onto feature-a
```

This is one-way (child → parent). To go the other direction, edit the parent directly and `pm stacker sync`.

## After a PR Is Merged

When the leaf PR merges (or any PR — pm doesn't enforce merge order):

```bash
pm repo pull                             # fetch master with the merge
pm stacker ls --json                     # tracked branches, with `merged: true` for the merged one
pm stacker remove --branch stack/feature-a   # drops it; children are reparented to feature-a's parent
pm stacker sync                          # replay remaining lineage onto the new master
pm stacker push
```

`pm stacker push` automatically skips branches that are already merged into the trunk, so it's safe to leave merged branches in place briefly.

## Common Mistakes

| Wrong | Right | Why |
|---|---|---|
| `git pull` on a `stack/*` branch | `pm stacker sync` | The git-guard hook blocks pull/rebase on tracked branches; sync is the correct way to incorporate parent changes. |
| `git rebase` on a `stack/*` branch | `pm stacker sync`, or `pm stacker reparent` to change parent | Same hook; rebase would desync managed-base from git's history. |
| `git push` / `git push --force` | `pm stacker push` | Direct push skips PR cache updates and stack-block rendering in the PR body. |
| `git branch -D stack/<x>` | `pm stacker remove --branch stack/<x>` | Direct delete leaves children orphaned with a missing parent in the stacker db. |
| `git cherry-pick --continue` after a sync conflict | `pm stacker continue` | pm needs to advance its queue, not just finish the one cherry-pick. |
| Pushing without syncing first | `pm stacker sync && pm stacker push` | Push doesn't replay parent changes; you'd push a stale base. |
| Reading state from human output | `pm stacker ls --json` | Tree rendering is for humans; JSON is parseable and stable. |
| Pasting commit SHAs in a PR description | Reference the branch by name | SHAs change on every sync. |
| Running `pm stacker sync` from `master` | Run sync from inside the stack | Sync from master with `--all` syncs every stack — usually not what you want. |

## What `pm stacker ls --json` Returns

Each branch entry includes the fields needed to plan an action:

```json
{
  "current_branch": "stack/feature-b",
  "branches": [
    {
      "repo_name": "frontend",
      "branch": "stack/feature-a",
      "parent_repo_name": "frontend",
      "parent_branch": "master",
      "is_root": true,
      "pr_url": "https://github.com/acme/frontend/pull/12345",
      "merged": false,
      "status": "synced",
      "needs_sync": false,
      "ahead_of_remote": 0,
      "commit_count": 1,
      "children": [
        {"repo_name": "frontend", "branch": "stack/feature-b", ...}
      ]
    }
  ]
}
```

Use:
- `needs_sync` → run `pm stacker sync`
- `ahead_of_remote > 0` → run `pm stacker push`
- `merged: true` → candidate for `pm stacker remove`
- `pr_url == null` → first push will create the PR
- `status` → `"local_only"`, `"synced"`, `"out_of_sync"`, `"merged"`, etc.

## When to Hand Off

If the user is doing project / worktree / pool / agent operations (anything that's not stacked branches) — that lives in the **`pm-workflow`** skill. This skill stops at the stacker boundary.
