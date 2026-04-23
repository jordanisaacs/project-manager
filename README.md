# project-manager (`pm`)

A CLI with three layers:

- **pool** — UUID-named git worktree slots per repo; reused across projects so checkouts stay warm. Ownership lives in a central SQLite db (`<pool>/.pool.db`) with a `PRIMARY KEY (repo, uuid)` — concurrent claimers race on `INSERT`, exactly one wins.
- **project** — named, multi-repo working set. A project dir holds a per-project SQLite db (which slots it remembers) plus forward symlinks (which slots are currently attached). Split lets `detach` unlink cleanly while `attach` reclaims the remembered slot.
- **stacker** — branch-stack tracking over the pool: cherry-pick sync, `gh` PR creation with stack-block rendering, and a `guard` that blocks `git pull` / `git rebase` on stacker-managed branches.

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
pm project new <name> --repos r1,r2       # create project, claim slots, link
pm project attach <name> {--repos … | --all}
pm project detach <name> {--repos … | --all}
pm project delete <name> [--repos …]      # per-repo or whole project
pm project ls | status <name>

pm pool ls [<repo>]                       # slots with claim status
pm pool add <repo>                        # mint a slot
pm pool gc-ops                            # release stacker-ops slots w/ no live op

pm stacker create <branch> [--on current|parent|<branch>] [--copy <b>] [--replace] [--no-checkout]
pm stacker sync [<branch>] [-c|-a] [--skip-ancestors] [--skip-descendants] [--from <b>] [--continue | --abort]
pm stacker push [<branch>] [-c|-a] [--only] [--skip-ancestors] [--skip-descendants] [--publish|--draft] [--create-pr true|false]
pm stacker ls [<branch>] [-c|-a] [--details none|status|status-counts|all] [--json]
pm stacker remove [<branch>] [--keep-branch] [--parent] [--force]
pm stacker reparent <new-parent> [--branch <b>] [--continue | --abort]
pm stacker split <new-name> <commit> [--stay]
pm stacker rename <new-name>
pm stacker log [<branch>]
pm stacker continue | abort                     # resume/cancel paused op
pm stacker config [--list | --unset] [<key> [<value>]]   # pr.mode, pr.trunk, pr.target-repo

pm check [--fix]                          # invariant scan across pool + projects
```

## Shell integration

Source `integrations/pm-git-guard.zsh` from `~/.zshrc` to block `git pull` / `git rebase` on stacker-tracked branches (fail-open outside pm slots, detached HEAD, or non-repos).

## Testing

`make check` (ruff + ty + pytest). Tests use an isolated `pm_env` tmp dir via `tests/conftest.py` — nothing touches real `~/.repos` / `~/.worktrees` / `~/.projects`.

## Acknowledgements

- `pool/worktree.py` ports Rodrigo Gomes' `wt` init logic (submodule `--reference`, CLAUDE copy, `init.sh`).
- The stacker layer is adapted from Rodrigo Gomes' standalone `stacker` (cherry-pick sync model, paused-op / continue-abort state machine, `guard no-rebase` hook).
- PR caching, stack-block rendering, and the per-PR "Files changed" view mirror universe `ci/gitstack`'s `StackItem` model.
