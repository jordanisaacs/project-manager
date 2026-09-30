# project-manager (`pm`)

A CLI for keeping git checkouts, named working sets, and stacked PRs in
sync across many repos. Four layers, each usable on its own:

- **pool** — a shared set of git worktree slots per repo. Slots are
  reused across projects so a fresh checkout is rarely needed; a slot
  carries warm git state, build caches, and editor history.
- **project** — a named, multi-repo working set (think
  "lakebase-replicator-azure-dnc"). A project pins one or more pool
  slots for the work it cares about, exposes them under
  `~/.projects/<name>/<repo>`, and tracks attach/detach so you can
  swap projects without losing context.
- **stacker** — branch-stack tracking on top of the pool. Cherry-pick
  sync, GitHub PR creation with stack-block rendering, and a
  `pm stacker push` that knows about ancestors and descendants.
- **repo** — the canonical clone under `~/.repos/<repo>` that every
  pool slot shares objects with. `pm repo pull` keeps trunk fresh,
  `pm repo maintenance` warms object stores and indexes.

## Install

```bash
uv sync      # adds `pm` to the venv; run via `uv run pm …`
```

## Config

`$PM_CONFIG` → `$XDG_CONFIG_HOME/pm/config.toml` → `~/.config/pm/config.toml` → defaults.

```toml
[paths]
repos = "~/.repos"          # canonical checkouts
worktrees = "~/.worktrees"  # pool slots
projects = "~/.projects"    # project dirs (db + forward symlinks)
```

## Commands

```
# project — named working sets
pm project create <name> --repos r1,r2     # create a project, claim slots, link
pm project ls | status <name>
pm project wt add <name> --repos …         # add worktrees to an existing project
pm project wt attach <name> {--repos … | --all}
pm project wt detach <name> {--repos … | --all}
pm project wt remove <name> [--repos …]
pm project delete <name> [--repos …]       # per-repo or whole project

# pool — worktree slots
pm pool ls [<repo>]                        # slots with claim status
pm pool add <repo>                         # mint a slot
pm pool delete <repo> <uuid> [--dry-run]   # safely delete one exact free slot

# repo — canonical clones
pm repo ls                                 # repos with branch + upstream
pm repo pull                               # fetch + ff-only every canonical clone
pm repo maintenance                        # warm git object store, index, fsmonitor

# stacker — branch-stack tracking
pm stacker create <branch> [--on current|parent|<branch>] [--copy <b>] [--replace] [--no-checkout]
pm stacker sync [<branch>] [-c|-a] [--skip-ancestors] [--skip-descendants] [--from <b>]
                [--allow-drop-parent-modifications] [--allow-drop-merge] [--offline] [--continue | --abort]
pm stacker push [<branch>] [-c|-a] [--only] [--skip-ancestors] [--skip-descendants] [--publish|--draft] [--create-pr true|false]
pm stacker ls [<branch>] [-c|-a] [--details none|status|status-counts|all] [--json]
pm stacker remove [<branch>] [--keep-branch] [--parent] [--force]
pm stacker reparent <new-parent> [--branch <b>] [--continue | --abort]
pm stacker split <new-name> <commit> [--stay]
pm stacker rename <new-name>
pm stacker absorb [--continue | --abort]
pm stacker log [--branch <branch>] [--repo <repo>]
pm stacker repair <base-ref> [--branch <branch>] [--repo <repo>]
pm stacker continue | abort                # resume/cancel paused op
pm stacker config [--list | --unset] [<key> [<value>]]   # pr.* and github.config-dir
pm stacker pr refresh                      # re-look-up the PR for the current branch

# agents — recent coding-agent sessions
pm agent ls                                # recent Claude / Codex / Cursor sessions
pm agent claude | codex | cursor [args…]   # launch one in the current project

# misc
pm cd <project> [<wt>]                     # cd into a project (or worktree); needs pm-cd.zsh
pm cd --print <project> [<wt>]             # print path instead — wrap with `cd "$(pm cd --print …)"`
pm check [--fix]                           # invariant scan across pool, projects, repos
```

### Safe pool slot deletion

Use the full repo and UUID from `pm pool ls --json`; deletion never does
prefix or fuzzy matching. Preview it first when removing a warm slot:

```bash
pm pool delete frontend 01234567-89ab-cdef-0123-456789abcdef --dry-run --json
pm pool delete frontend 01234567-89ab-cdef-0123-456789abcdef --json
```

The dry-run exits 0 when deletion is safe and 1 when it reports a blocker.
The live command atomically reserves the slot, then refuses project/stacker
claims, unregistered or locked worktrees, in-progress operations, tracked or
untracked files, and detached commits not reachable from a branch, remote ref,
or tag. On success it removes both the Git worktree registration and slot path.

### Stacker branch targeting and checkouts

Explicit branch selection does not imply that the branch must already be in
the current worktree. The command audit follows these rules:

| Commands | Checkout behavior |
|---|---|
| `ls`, `log --branch`, `pr refresh --branch`, `pr unlink --branch`, `remove --keep-branch` | Read refs or local metadata directly; the target need not be checked out. |
| `repair --branch` | Resolves refs (including `HEAD~N`) against the target branch without checkout. If the target is checked out, that worktree must be clean. |
| `sync --branch`, `push --branch`, `absorb --branch`, `reparent --branch` | Work while cwd is detached or on another branch; they locate or acquire the worktree needed for mutations. |
| `remove --branch` | Does not require a prior checkout; if the branch is live elsewhere, removal detaches that worktree before deleting the ref. |
| `rename --branch`, `split --branch` | Require the target checkout because they deliberately rename/reset that working tree. |
| `create --on current\|parent`, `guard no-rebase` | Require current-checkout context by definition. Literal `--on <branch>` with `--repo` does not. |
| `continue`, `abort` | Require the worktree holding the active operation so conflict state can be resumed or restored. |

Outside a pm slot, pass `--repo`; a detached pm slot can still supply the repo
while an explicit `--branch` supplies the target.

### Submodule lifecycle

PM-managed checkouts, hard resets, successful cherry-picks, project
add/detach/attach/remove/delete, and stacker slot acquisition/release synchronize
`.gitmodules` URLs and run recursive `submodule update --init --checkout`.
This includes nested and newly-added submodules, so changing a parent gitlink
does not leave a released or reattached slot falsely dirty. Genuine submodule
work remains protected by the existing dirty-worktree checks.

When a pool slot is first created, PM checks the canonical checkout and existing
slots for URL-compatible submodule repositories (including stored module
gitdirs). It fetches the exact pinned commit locally when available, recursively,
without copying worktree/index state or retaining an alternate dependency. The
configured submodule remote is used only when no usable local store has that
commit.

### Sync semantics

`pm stacker sync` always cherry-picks exactly the commits the branch
added on top of its recorded base (`managed_base..HEAD`). Two preflight
gates protect that replay:

- `--allow-drop-parent-modifications` — required when the branch's
  history *below* the stack range was rewritten outside stacker (e.g.
  `git rebase --onto`, force-push from elsewhere, amends touching
  pre-stack commits). Without the flag, sync errors; with it, those
  changes are dropped and only the working commits are replayed onto
  the parent's current tip.

- Merged-PR collapse — when a branch's PR is `MERGED`, sync skips the
  cherry-pick entirely and resets the branch to its parent's tip. The
  branch becomes a no-commit branch that children stack on transparently.
  If the squash isn't actually on the parent yet (PR was merged into a
  different base, or the local parent hasn't pulled the merge),
  `--allow-drop-merge` is required to perform the same reset anyway.

By default sync refreshes cached `pr_state` once up front (one batched
GraphQL call per repo). `--offline` skips that refresh and reads
whatever's already cached.

### Per-repo GitHub CLI identity

Stacker can select a separate GitHub CLI configuration for each PM repo:

```bash
pm stacker config --repo frontend github.config-dir ~/.config/gh-frontend
```

The configured directory is passed as `GH_CONFIG_DIR` to Stacker's `gh`
subprocesses. Git pushes keep using the repository's normal Git credential
configuration. An inherited `GH_TOKEN` or `GITHUB_TOKEN` is preserved and
therefore retains the GitHub CLI's usual precedence over stored credentials.

## Integrations

`integrations/` holds optional plug-ins for the surrounding tooling.

### Omnigent project sync

PM can mirror its named projects into an Omnigent server. The integration is
off unless an `[omnigent]` table is present in PM's normal config file
(`$PM_CONFIG`, `$XDG_CONFIG_HOME/pm/config.toml`, or
`~/.config/pm/config.toml`):

```toml
[omnigent]
server_url = "http://localhost:6767"
host_id = "0123456789abcdef0123456789abcdef"
```

`host_id` is the Omnigent host that can access the PM project directories. For
a local `omnigent host`, it is `host.host_id` in
`~/.omnigent/config.yaml`; it is also returned by `GET /v1/hosts`. Each synced
Omnigent project receives these defaults:

```json
{
  "host_id": "<configured host_id>",
  "workspace": "<absolute path to ~/.projects/project-name>"
}
```

PM merges those two keys into the project's existing Omnigent config, so
defaults managed in Omnigent (such as `agent_id`, `model`, or `base_branch`)
survive later syncs.

For a server requiring a bearer token, keep the secret out of TOML and name an
environment variable:

```toml
[omnigent]
server_url = "https://omnigent.example.com"
host_id = "0123456789abcdef0123456789abcdef"
token_env = "PM_OMNIGENT_TOKEN"
```

```bash
export PM_OMNIGENT_TOKEN="..."
```

For renewable credentials, configure an argv-style command instead. PM accepts
either a plain token on stdout or JSON containing `access_token` / `token`:

```toml
[omnigent]
server_url = "https://workspace.example.com/api/2.0/omnigent"
host_id = "0123456789abcdef0123456789abcdef"
token_command = ["databricks", "auth", "token", "--profile", "YOUR_PROFILE"]
timeout_seconds = 15
```

`token_env` and `token_command` are mutually exclusive. Omit both for an
unauthenticated local server. To disable a retained configuration without
deleting it, set `enabled = false`.

Backfill existing projects and verify the result:

```bash
pm omnigent sync --json
pm project create omnigent-sync-check
pm omnigent sync --json
```

The first command creates or adopts same-name Omnigent projects. The second
demonstrates automatic sync for future CLI-created projects; `pm project
delete` automatically removes its mapped Omnigent project. Automatic sync is
best-effort: a network/auth failure prints a warning but never rolls back a
successful local create/delete. The explicit sync exits non-zero when any item
fails and retries pending work.

Identity mappings and deletion tombstones live in
`<projects-root>/.pm-omnigent.db`. This makes repeated syncs idempotent and
allows a failed remote deletion to be retried after the local directory is
gone. PM only deletes remote IDs recorded in this database; unrelated
Omnigent-only projects are never pruned. If a mapped project is renamed or
deleted in Omnigent, the next sync restores the PM name or recreates it. PM
does not currently support renaming local projects; use its supported
create/delete lifecycle rather than moving a project directory by hand.

Check the Omnigent UI or query the API after setup:

```bash
curl -sS \
  -H "Authorization: Bearer $PM_OMNIGENT_TOKEN" \
  "$OMNIGENT_URL/v1/projects" | jq '.data[] | {name, config}'
```

For an unauthenticated local server, omit the `Authorization` header.

### Shell (zsh)

- **`pm-git-guard.zsh`** — source from `~/.zshrc`. Blocks `git pull` /
  `git rebase` on stacker-tracked branches (use `pm stacker sync`
  instead). Fail-open outside pm slots, on detached HEAD, or in
  non-repos.
- **`pm-cd.zsh`** — source from `~/.zshrc` to make `pm cd <project>
  [<wt>]` actually change directory. The Python program can only
  print; the zsh wrapper does the `cd`. `pm cd --print …` opts out
  for scripting.
- **Tab completion** — add `integrations/` to `$fpath` before
  `compinit`:

  ```zsh
  fpath=(/path/to/project-manager/integrations $fpath)
  autoload -Uz compinit && compinit
  ```

  Completes project names, repo names, and worktree names (including
  comma-separated `--wt foo,bar,...` lists) by shelling out to
  `pm __complete`. `pm stacker …` is covered at subcommand-name
  granularity only.

### Claude Code skill (`integrations/claude-code/`)

A Claude Code plugin that teaches the model the `pm` mental model so
it can drive projects, slots, and stacker branches without a guided
tour each session. Installable from the experimental marketplace —
see `integrations/claude-code/README.md` for the install snippet.

### Emacs (`integrations/emacs/`)

`pm.el` recognizes pm containers (`~/.projects/<name>/`) as
first-class `project.el` projects, exposes magit-style transients
(`C-x p P` global, `C-x p .` for the current project), and treats
every git worktree under a container as a "subproject". See
`integrations/emacs/README.md` for setup.

## Testing

`make check` (ruff + ty + pytest). Tests use an isolated `pm_env` tmp
dir via `tests/conftest.py` — nothing touches real `~/.repos` /
`~/.worktrees` / `~/.projects`.

## Acknowledgements

The stacker layer is adapted from Rodrigo Gomes' standalone `stacker` — cherry-pick sync model, paused-op / continue-abort state machine, `guard no-rebase` hook.
