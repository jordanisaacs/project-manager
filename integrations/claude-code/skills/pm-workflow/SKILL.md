---
name: pm-workflow
description: |
  Workflow for the `pm` CLI — managing named projects, pooled git worktree slots, and AI agent sessions.
  Use when the user runs `pm`, `pm project`, `pm pool`, `pm repo`, `pm agent`, or `pm cd`, or when the
  working directory is under ~/.projects/ or ~/.worktrees/.
  Triggers: "pm", "pm cd", "pm project", "pm pool", "pm repo", "pm agent", "worktree slot",
  "switch project", "~/.projects/", "~/.worktrees/"
---

# pm Workflow

Reference for using the `pm` CLI to manage named projects, pooled worktree slots, and AI agent sessions. Use when the user runs `pm` (anything other than `pm stacker` — see the `pm-stacker-workflow` skill for that).

## Mental Model

- A **project** is a named folder under `~/.projects/<name>/` containing symlinks to git worktrees plus a small `.pm.db`. Think of it as a labelled bag of checkouts you can `cd` into.
- A **worktree** (`wt`) is a named symlink inside a project (e.g. `universe`, `runtime`) pointing at a real git checkout.
- A **slot** is the actual checkout, stored under `~/.worktrees/<repo>/<uuid>/`. Slots are pooled and stay warm across project switches — users never type the UUID, they only ever name the worktree.
- A **repo** is the canonical clone under `~/.repos/<name>/`. Slots are spawned from it.

When you detach a worktree the symlink disappears but the slot stays in the pool, ready to be re-attached. The same slot can be reused by a different project later.

## Critical Rules

1. **Always pass `--json` when reading state.** `pm project ls`, `pm project status`, `pm pool ls`, `pm repo ls`, `pm agent ls`, `pm check`, and the `pm project wt` plan-output commands all support `--json`. Use it for any inspection — the human-readable output uses tree/table rendering meant for terminals.
2. **Don't edit symlinks under `~/.projects/<name>/` by hand.** Always go through `pm project wt create | attach | detach | delete`. Hand-edits desync the project's `.pm.db` and the pool.
3. **Don't `git init` or commit inside a project directory.** Projects are not git repos; they're folders of symlinks. Run git commands inside the worktree (e.g. `~/.projects/foo/universe/`), not the project root.
4. **`pm cd` only works if the zsh wrapper is sourced.** It needs `source <pm install>/integrations/pm-cd.zsh` in the user's zshrc. Without the wrapper, `pm cd` prints help. Use `pm cd --print <proj> [<wt>]` if you just need the path for scripting.
5. **Use `pm check --fix` to repair drift**, never manual surgery on symlinks or the pool db.
6. **Don't create projects or worktrees** unless the user asked. `pm project create` and `pm project wt create` are durable, user-visible state.

## Command Reference

### Project lifecycle

| Command | Description | `--json` |
|---|---|---|
| `pm project ls` | List all projects with their worktrees and statuses | ✓ |
| `pm project status <name>` | Detailed health for one project (worktrees, branches, PRs, agent sessions) | ✓ |
| `pm project create <name> [--wt <spec>]` | Create a new project, optionally with initial worktrees | ✓ |
| `pm project delete <name> [--dry-run]` | Delete a project (releases all its slots) | ✓ |

### Worktrees within a project

| Command | Description | `--json` |
|---|---|---|
| `pm project wt create <repo> [-p <proj>] [--spec <spec>]` | Add worktrees to a project | ✓ |
| `pm project wt attach [-p <proj>] [--wt a,b \| --all]` | Re-attach worktrees, reclaim slots from the pool | ✓ |
| `pm project wt detach [-p <proj>] [--wt a,b \| --all] [--dry-run]` | Unlink the symlinks but keep the slots warm in the pool | ✓ |
| `pm project wt delete [-p <proj>] [--wt a,b \| --all] [--dry-run]` | Remove worktrees from the project entirely | ✓ |

`-p/--project` is optional — if omitted, `pm` infers the project from the cwd.

### Pool

| Command | Description | `--json` |
|---|---|---|
| `pm pool ls [<repo>]` | List slots with claim status (FREE or owning project) | ✓ |
| `pm pool add <repo>` | Mint a fresh slot for a repo (pre-warm before claiming) | ✓ |

### Repos (canonical clones)

| Command | Description | `--json` |
|---|---|---|
| `pm repo ls` | List canonical repos and their HEAD/upstream status | ✓ |
| `pm repo pull` | Fetch + ff-only pull each canonical repo | ✓ |
| `pm repo maintenance` | Run `git gc` / fsmonitor / index across repos and slots (also fired by the systemd timer) | ✓ |

### Agents

| Command | Description | `--json` |
|---|---|---|
| `pm agent ls [--project <p>] [--agent <name>] [--limit N] [--resume]` | List recent AI agent sessions | ✓ |
| `pm agent claude [--project <p>] -- <args...>` | Launch Claude Code in a project worktree |  |
| `pm agent codex [--project <p>] -- <args...>` | Launch Codex in a project worktree |  |
| `pm agent cursor [--project <p>] -- <args...>` | Launch Cursor in a project worktree |  |

### Navigation & health

| Command | Description | `--json` |
|---|---|---|
| `pm cd <project> [<wt>]` | cd to a project root (or a specific worktree). Requires `pm-cd.zsh` sourced. |  |
| `pm cd --print <project> [<wt>]` | Print the resolved path instead of cd'ing (for scripts) |  |
| `pm check [--fix]` | Verify pool / project invariants; `--fix` auto-repairs drift | ✓ |

## Common Workflows

### Starting a new project

```bash
pm project create my-feature --wt universe,runtime
pm cd my-feature                  # land in ~/.projects/my-feature
pm project status my-feature --json
```

`--wt universe,runtime` claims a slot per repo. To name worktrees explicitly: `--wt main:universe,svc:runtime`.

### Switching projects / worktrees

```bash
pm project ls --json              # inspect first
pm cd other-project               # to project root
pm cd other-project universe      # straight to a specific worktree
```

`pm cd` without arguments isn't valid — always pass at least the project name.

### Adding a worktree to an existing project

```bash
pm project wt create universe -p my-feature
# Inferred project from cwd:
cd ~/.projects/my-feature && pm project wt create runtime
```

### Detach / re-attach (parking a slot without losing the branch)

```bash
pm project wt detach -p my-feature --wt universe       # symlink gone, slot warm in pool
pm pool ls universe --json                             # confirm slot is FREE
pm project wt attach -p my-feature --wt universe       # reclaim same slot if free
```

Detach preserves the branch (saved in `.pm.db`); attach restores it.

### Cleaning up

```bash
pm project wt delete -p my-feature --wt scratch --dry-run   # preview
pm project wt delete -p my-feature --wt scratch
# Or wipe the whole project:
pm project delete my-feature --dry-run
pm project delete my-feature
```

### Launching an AI agent in a project

```bash
pm agent claude --project my-feature
pm agent codex --project my-feature -- --resume
pm agent ls --project my-feature --json     # see prior sessions
```

### Health check

```bash
pm check --json                  # report drift (orphaned symlinks, pool mismatches)
pm check --fix                   # auto-repair
```

## Inspecting State (Always JSON)

For any "what's the current state?" question, use `--json` and parse:

```bash
pm project ls --json
pm project status <name> --json
pm pool ls --json
pm pool ls <repo> --json
pm repo ls --json
pm agent ls --json
pm check --json
pm stacker ls --json     # see the pm-stacker-workflow skill
```

`pm project status <name> --json` returns a structured object covering worktrees, branch state, PR cache, and agent sessions — it's the most useful single inspection command.

`pm pool ls --json` distinguishes free slots (`"status": "FREE"`) from claimed ones (`"status": "<project-name>"` or `"status": "stacker:ops"`).

## Tripping Hazards

| Footgun | What happens | Right move |
|---|---|---|
| Editing symlinks under `~/.projects/<n>/` by hand | Project `.pm.db` and pool drift | `pm project wt {attach,detach,delete}`, then `pm check --fix` |
| `git init` or committing inside a project root | The project becomes a broken pseudo-repo | Run git inside the worktree (e.g. `~/.projects/p/universe/`), never the project root |
| Detach refuses with "uncommitted changes" or "in-progress operation" | Slot has dirty state or a paused stacker op | Resolve in the worktree first (commit/stash, or `pm stacker continue`/`abort`) |
| `pm cd` prints help instead of cd'ing | The zsh wrapper isn't sourced | Source `<install>/integrations/pm-cd.zsh` in zshrc; or use `pm cd --print` for the path |
| `PoolExhaustedError` on create/attach | No free slots for that repo and the pool can't grow on demand | `pm pool add <repo>` to mint a slot, or `pm project wt detach` an unused worktree to free one |
| "slot busy" on detach/delete | Slot has a paused `pm stacker` operation holding it | `pm stacker continue` or `pm stacker abort` in the affected worktree first |

## Output Samples (for parsing)

`pm project ls --json` returns:

```json
[
  {
    "project": "my-feature",
    "worktrees": [
      {"project": "my-feature", "wt": "universe", "repo": "universe",
       "branch": "stack/foo", "status": "attached"}
    ]
  }
]
```

`pm pool ls --json` returns one row per slot with `repo`, `uuid`, `branch`, and `status` (either `"FREE"` or the owning project name / `"stacker:ops"`).

For the human-readable form (when relaying to the user), `pm project ls` renders a grouped table and `pm pool ls` shows columns `Repo | UUID | Branch | Status`.

## When to Hand Off

If the user is doing anything with stacked branches (`pm stacker ...`, `stack/*` branches, "sync the stack", "push my stack") — that lives in the **`pm-stacker-workflow`** skill. This skill stops at slot/worktree management.
