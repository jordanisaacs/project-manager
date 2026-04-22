# project-manager (`pm`)

A small CLI that manages a claim-release pool of git worktrees and binds them to named projects via symlinks.

## What it does

- **Pool**: stable UUID-named worktree slots under `~/.worktrees/<repo>/<uuid>/`. Reused across projects — checking out another branch does not re-incur cold-build cost.
- **Projects**: a project is a directory `~/.projects/<project>/` holding a per-project SQLite db (`.pm.db`) and, when attached, forward symlinks `<repo> -> <slot>`.
- **Two sources of truth**: the db records *which repos belong to this project* and *which slot each was last bound to*. Symlinks record *currently attached*. Separating these lets `detach` cleanly unlink while `attach` still knows which slot to reclaim.
- **Atomic claim**: ownership is recorded by a reverse symlink `<slot>/.owner` created via `ln -s`, which is POSIX-atomic — two concurrent claimers racing for the same slot, exactly one wins.
- **No branch management**: `pm` does not touch branches or stacks. Whatever checkout state the slot already has is inherited. Layer `git-stack` / Rodrigo's `stacker` on top as you wish.

## Install

```bash
uv sync
```

Adds `pm` to the venv. Activate the venv or run commands through `uv run pm …`.

## Paths (configurable)

| Role | Default | Override |
|---|---|---|
| canonical repo checkouts | `~/.repos/<repo>/` | `paths.repos` |
| pool slots | `~/.worktrees/<repo>/<uuid>/` | `paths.worktrees` |
| forward symlinks | `~/.projects/<project>/<repo>` | `paths.projects` |

Config resolution order: `$PM_CONFIG` → `$XDG_CONFIG_HOME/pm/config.toml` → `~/.config/pm/config.toml` → built-in defaults. Schema:

```toml
[paths]
repos = "~/.repos"
worktrees = "~/.worktrees"
projects = "~/.projects"
```

## Commands

```
pm project new <name> --repos r1,r2,...             # create project, claim slots, link
pm project attach <name> {--repos r1,.. | --all}    # best-effort reclaim remembered slot
pm project detach <name> {--repos r1,.. | --all}    # unlink forward + release .owner
pm project delete <name> [--repos r1,..]            # per-repo (implicit detach + drop row)
                                                    # or whole project (detach all, drop db, rmdir)
pm project ls                                       # rows from each project's db + status
pm pool ls [<repo>]                                 # pool slots with claim status
pm pool add <repo>                                  # mint a new slot (git worktree add --detach)
pm check [--fix]                                    # invariant scan; --fix reconciles
```

`attach` and `detach` require exactly one of `--repos` or `--all`; there is no implicit default.

### Example

```bash
# Mint two slots for universe
pm pool add universe
pm pool add universe

pm pool ls universe
# universe <uuid-1> FREE
# universe <uuid-2> FREE

# Create a project using one slot
pm project new my-feature --repos universe
ls -la ~/.projects/my-feature/
# .pm.db
# universe -> ~/.worktrees/universe/<uuid-1>

# Detach — slot is back in the pool, forward symlink is unlinked; .pm.db stays
pm project detach my-feature --all
pm pool ls universe
# universe <uuid-1> FREE
# universe <uuid-2> FREE
ls ~/.projects/my-feature/
# .pm.db   (project dir now holds only the db)

# Attach — best-effort reclaim of the remembered slot
pm project attach my-feature --all
# my-feature/universe -> ~/.worktrees/universe/<uuid-1>   (same slot: warm!)

# Per-repo delete (implicit detach)
pm project delete my-feature --repos universe

# Whole-project delete (fails if non-pm entries exist)
pm project delete my-feature
```

## Invariants (`pm check`)

For each project identified by presence of `<projects>/<name>/.pm.db`:

| State | Meaning | `--fix` |
|---|---|---|
| **active** | db row + forward symlink + `.owner` matches; uuid matches db | — |
| **detached** | db row, no forward symlink | — (intentional) |
| **drift** | forward points at a different slot than db remembers; `.owner` matches | — (informational) |
| **stale** | forward exists but `.owner` is missing or points at a different forward | unlink forward |
| **broken** | forward points at a missing slot dir | unlink forward |
| **orphan-forward** | forward exists with no db row in the project | unlink forward |
| **orphan-owner** | slot `.owner` points at a forward not backed by any active db row | remove `.owner` |

## Testing

```bash
make check      # ruff + ty + pytest
make lint       # just ruff
make typecheck  # just ty (strict via [tool.ty.terminal] error-on-warning)
make test       # just pytest
make fmt        # ruff format + auto-fix
```

Tests run in isolated `tmp_path` via the `pm_env` fixture — nothing touches real `~/.repos`, `~/.worktrees`, or `~/.projects`.

## Layout

```
src/project_manager/
├── cli.py            # top-level argparse
├── config.py         # TOML loader
├── paths.py          # Paths(repos, worktrees, projects) dataclass
├── check.py          # invariant scan + --fix
├── project/
│   ├── cli.py
│   ├── db.py         # per-project sqlite helpers (transaction() ctx mgr)
│   ├── new.py        # create db + rows, claim slots, forward-link, rollback
│   ├── attach.py     # read db, best-effort reclaim remembered slot else fallback + UPDATE
│   ├── detach.py     # unlink forward + release .owner; rows untouched
│   ├── delete.py     # per-repo (detach + DELETE row) or whole (detach + drop db + rmdir)
│   └── ls.py
└── pool/
    ├── cli.py
    ├── slot.py       # atomic claim()/release() via os.symlink
    ├── add.py        # mint a new UUID slot
    ├── ls.py
    └── worktree.py   # git worktree add, submodule init, CLAUDE copy, init.sh
```

## Acknowledgements

`pool/worktree.py` ports the submodule-init-with-`--reference`, CLAUDE-file copy, and `init.sh` runner from Rodrigo Gomes' standalone `wt` script (`experimental/rodrigo-gomes_data/wt/wt` @ `ab3b42e526cf8` in databricks-eng/universe).
