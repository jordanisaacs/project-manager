# project-manager (`pm`)

A small CLI that manages a claim-release pool of git worktrees and binds them to named projects via symlinks.

## What it does

- **Pool**: stable UUID-named worktree slots under `~/.worktrees/<repo>/<uuid>/`. Reused across projects — checking out another branch does not re-incur cold-build cost.
- **Projects**: a project is a directory of forward symlinks at `~/.projects/<project>/<repo>`, each pointing at a claimed pool slot.
- **Atomic claim**: a slot is "owned" by the project whose forward symlink it's bound to. Ownership is recorded by a reverse symlink `<slot>/.owner` created via `ln -s`, which is POSIX-atomic — two concurrent claimers racing for the same slot, exactly one wins.
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
pm project new <name> --repos r1,r2,...   # claim a free slot per repo
pm project release <name>                 # drop .owner, keep forward symlinks
pm project attach <name>                  # inverse of release: re-claim slots for existing forwards
pm project delete <name>                  # release + remove forward symlinks + rmdir
pm project ls                             # active projects and their bindings
pm pool ls [<repo>]                       # pool slots with claim status
pm pool add <repo>                        # mint a new slot (git worktree add --detach)
pm check [--fix]                          # invariant scan; --fix reconciles orphans/broken
```

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
# my-feature/universe -> ~/.worktrees/universe/<uuid-1>

# Release — slot is back in the pool, forward symlink stays for browsing
pm project release my-feature
pm pool ls universe
# universe <uuid-1> FREE
# universe <uuid-2> FREE

# Delete — remove the project entirely (fails if extra files exist)
pm project delete my-feature
```

## Invariants (`pm check`)

For every slot `S = <worktrees>/<repo>/<uuid>/`:
- If `S/.owner` exists, it must point at a forward symlink that points back at `S`. Otherwise → **orphan**.

For every forward `P = <projects>/<name>/<repo>`:
- `P` must point at an existing slot `S`. Otherwise → **broken**.
- If `S/.owner` points back at `P` → **active** (healthy).
- If `S/.owner` is absent → **detached** (post-release state; intentional).
- If `S/.owner` points at a different forward → **stale** (slot was re-claimed after this project released it).

`pm check --fix` reconciles orphans and broken entries. Detached and stale are intentional states and left alone.

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
│   ├── new.py        # claim + forward-link with rollback
│   ├── release.py    # drop .owner
│   ├── delete.py     # release + remove forward symlinks + rmdir
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
