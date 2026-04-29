# pm Plugin

Claude Code skills for the `pm` CLI — manage named projects, pooled worktree slots, and cherry-pick-based stacked PRs.

## Installation

Once published to a marketplace:

```bash
isaac plugin add pm@experimental
```

## Skills

This plugin ships two skills that activate on different surfaces:

### `pm-workflow`

Project + worktree + pool + agent UX. Activates when the user asks about `pm`, `pm cd`, `pm project`, `pm pool`, `pm repo`, `pm agent`, or when the working directory is under `~/.projects/` or `~/.worktrees/`.

Teaches the agent:

- How to create / list / switch projects and their worktrees
- The attach/detach/delete lifecycle of pooled slots
- When to use `pm cd` (and that it requires the zsh wrapper)
- Launching AI agents (`pm agent claude|codex|cursor`)
- How to inspect state with `pm project status --json` and friends

### `pm-stacker-workflow`

Stacked-PR workflow driven by `pm stacker`. Activates when the user asks about `pm stacker`, when on a `stack/*` branch, or when discussing stacked PRs in a `pm`-managed repo.

Teaches the agent:

- The cherry-pick replay model (SHAs change on every sync)
- The `create → commit → sync → push` daily loop
- How `pm-git-guard` blocks `git pull` / `git rebase` and what to use instead
- Conflict resolution via `pm stacker continue` / `pm stacker abort`
- Reparent, split, absorb, and after-merge cleanup
- Differences from rebase-based tools like `git stack` (for transitioning users)

## Key Rules Enforced

- Always pass `--json` when reading state with `pm` (e.g. `pm stacker ls --json`)
- Never `git pull` or `git rebase` on a `stack/*` branch — use `pm stacker sync`
- Never `git branch -D` a tracked branch — use `pm stacker remove`
- Always `pm stacker sync` before `pm stacker push`
- Don't manually edit symlinks under `~/.projects/<name>/` — use `pm project wt attach|detach|delete`

## Triggers

- Any `pm` or `pm stacker` command
- Working directory under `~/.projects/<name>/` or `~/.worktrees/<repo>/<uuid>/`
- Branch name matching `stack/*`
- Phrases: "stacked PR", "worktree slot", "pm cd"

## Quick Reference

```bash
# Project / worktree
pm project ls --json
pm project status <name> --json
pm project create <name> --wt <repo>[,<repo>...]
pm project wt attach|detach|delete -p <name> --wt <wt>
pm cd <project> [<wt>]

# Pool / repo
pm pool ls --json
pm repo ls --json && pm repo pull

# Stacker
pm stacker ls --json
pm stacker create stack/<name> --on <parent>
pm stacker sync
pm stacker push
pm stacker continue   # after resolving cherry-pick conflicts
pm stacker abort
```
