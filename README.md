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
pm project wt create <name> --repos …      # add worktrees to an existing project
pm project wt attach <name> {--repos … | --all}
pm project wt detach <name> {--repos … | --all}
pm project wt delete <name> [--repos …]
pm project delete <name> [--repos …]       # per-repo or whole project

# pool — worktree slots
pm pool ls [<repo>]                        # slots with claim status
pm pool add <repo>                         # mint a slot

# repo — canonical clones
pm repo ls                                 # repos with branch + upstream
pm repo pull                               # fetch + ff-only every canonical clone
pm repo maintenance                        # warm git object store, index, fsmonitor

# stacker — branch-stack tracking
pm stacker create <branch> [--on current|parent|<branch>] [--copy <b>] [--replace] [--no-checkout]
pm stacker sync [<branch>] [-c|-a] [--skip-ancestors] [--skip-descendants] [--from <b>] [--hard] [--continue | --abort]
pm stacker push [<branch>] [-c|-a] [--only] [--skip-ancestors] [--skip-descendants] [--publish|--draft] [--create-pr true|false]
pm stacker ls [<branch>] [-c|-a] [--details none|status|status-counts|all] [--json]
pm stacker remove [<branch>] [--keep-branch] [--parent] [--force]
pm stacker reparent <new-parent> [--branch <b>] [--continue | --abort]
pm stacker split <new-name> <commit> [--stay]
pm stacker rename <new-name>
pm stacker absorb [--continue | --abort]
pm stacker log [<branch>]
pm stacker continue | abort                # resume/cancel paused op
pm stacker config [--list | --unset] [<key> [<value>]]   # pr.mode, pr.trunk, pr.target-repo
pm stacker pr refresh                      # re-look-up the PR for the current branch

# agents — recent coding-agent sessions
pm agent ls                                # recent Claude / Codex / Cursor sessions
pm agent claude | codex | cursor [args…]   # launch one in the current project

# misc
pm cd <project> [<wt>]                     # cd into a project (or worktree); needs pm-cd.zsh
pm cd --print <project> [<wt>]             # print path instead — wrap with `cd "$(pm cd --print …)"`
pm check [--fix]                           # invariant scan across pool, projects, repos
```

## Integrations

`integrations/` holds optional plug-ins for the surrounding tooling.

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
