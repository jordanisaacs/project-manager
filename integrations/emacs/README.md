# pm.el — Emacs integration for `pm`

Drives the `pm` CLI from inside Emacs. Recognizes pm containers
(`~/.projects/<name>/`) as first-class `project.el` projects, exposes
two magit-style transients — `pm-dispatch` (global, `C-x p P`) and
`pm-project-dispatch` (current project, `C-x p .`) — and treats every
git worktree under a container as a "subproject" of that container.

## Requirements

- Emacs 28.1 or newer (built-in `transient`, `cl-defmethod`).
- `magit-section` 4.0 or newer — the standalone library, not full
  magit. Available on MELPA / NonGNU ELPA. Used by every
  status / list / pool / repo / agent buffer.
- `pm` on `$PATH`.

## Install

The package is shipped from this repo. Add a `use-package` block to your
Emacs config pointing at the directory:

```elisp
(use-package pm
  :load-path "~/.repos/project-manager/integrations/emacs/"
  :commands (pm-dispatch pm-project-dispatch pm-agent-launch-dispatch
             pm-refresh pm--project-finder pm-jump-parent pm-jump-sibling)
  :init
  (with-eval-after-load 'project
    (add-hook 'project-find-functions #'pm--project-finder)
    (add-to-list 'project-switch-commands
                 '(pm-project-dispatch "PM" ?P) t)
    ;; `?a' after `C-x p p <project>': launch a fresh agent in the
    ;; just-picked project (claude / codex / cursor).
    (add-to-list 'project-switch-commands
                 '(pm-agent-launch-dispatch "Agent" ?a) t))
  (global-set-key (kbd "C-x p P") #'pm-dispatch)
  (global-set-key (kbd "C-x p .") #'pm-project-dispatch)
  (add-hook 'after-init-hook
            (lambda () (run-with-idle-timer 0.5 nil #'pm-refresh))))
```

Adjust `:load-path` if the repo lives elsewhere.

## Usage

### `C-x p P` — global pm

Grouped by noun (Project / Pool / Repos / Agent), so every action
against a given resource sits in one column. The Agent group's `r`
enters the agent-launch sub-dispatch (claude / codex / cursor); since
there's no bound project at the global level, it prompts.

| Group   | Key | Command                                               |
| ------- | --- | ----------------------------------------------------- |
| Project | `c` | create                                                |
|         | `p` | switch                                                |
|         | `l` | list (magit-section)                                  |
| Pool    | `+` | mint a slot                                           |
|         | `o` | list                                                  |
| Repos   | `F` | pull all                                              |
|         | `R` | list                                                  |
| Agent   | `r` | run → (sub-dispatch: claude / codex / cursor)         |
|         | `a` | recent sessions across projects                       |
| System  | `g` | refresh                                               |
| Project | `.` | enter `pm-project-dispatch` (only shown in a pm tree) |

### `C-x p .` — current pm project

Header shows `pm: <container>` (or `<container>/<alias>` from inside a
worktree). If invoked outside any pm tree, prompts for a project and
re-enters bound to it. Use a prefix arg (`C-u C-x p .`) to force a
prompt even when in a pm tree.

| Group     | Key  | Toggle / Command                              |
| --------- | ---- | --------------------------------------------- |
| Arguments | `-A` | `--all` (batch wt ops)                        |
|           | `-n` | `--no-branch` (attach skips branch restore)   |
|           | `-d` | `--dry-run` (detach simulation)               |
| Inspect   | `s`  | status                                        |
| Worktrees | `n`  | new                                           |
|           | `a`  | attach                                        |
|           | `D`  | detach                                        |
|           | `X`  | delete worktree                               |
| Navigate  | `p`  | parent (when inside a worktree)               |
|           | `j`  | sibling (when ≥ 1 sibling exists)             |
| Agent     | `r`  | run → (sub-dispatch: claude / codex / cursor) |
|           | `l`  | sessions for this project                     |
| System    | `g`  | refresh                                       |
|           | `<`  | back to global pm                             |
| Danger    | `K`  | delete project                                |

### Agent-launch sub-dispatch

Reachable as `r` from either parent transient and as
`pm-agent-launch-dispatch` standalone (designed to bind under
`project-switch-commands`, e.g. `?a`). The sub-dispatch is fresh-launch
only — to resume a session, use `l` from the project transient (or
`a` from the global one) to open `*pm-agent-ls*` and `RET` on a row.

| Key | Command |
| --- | ------- |
| `c` | claude  |
| `o` | codex   |
| `u` | cursor  |
| `<` | back to project dispatch |

The `[Navigate]` group hides at the container level (no parent /
sibling there). After `j` lands you in a sibling worktree, use the
standard `C-x p f` for find-file — there's no dedicated find-file-in-sibling.

Suffix descriptions live-update as you toggle infix args, so e.g. with
`-A` enabled, the `D` line reads `detach (all)` and skips the
worktree prompt.

Every `pm` invocation is async; non-zero exits surface via
`display-warning` and a kept ` *pm-err: <verb>*` buffer (hidden from
`list-buffers` by its leading space; reachable via `M-x switch-to-buffer`).

### Buffers

All listing / status surfaces are `magit-section` buffers. Movement
(`n`/`p`/`M-n`/`M-p`), folding (`TAB`/`S-TAB`), and section ancestry
behave exactly like in magit.

#### Theming

Every buffer renders through a shared, magit-styled vocabulary
(`pm-table.el` + `pm-faces.el`) so they look consistent and pick up
your theme:

- **Banner** (`pm-buffer-title`) → **section heading** with a dim
  `(N)` count (`pm-section-heading` + `pm-count`) → **group heading**
  for a repo/project (`pm-group-heading`) → **column header**
  (`pm-column-header`, a quiet dim ruler) → rows. The column header is
  now its own face rather than reusing the section-heading face, so the
  hierarchy reads clearly.
- **Value columns are themed**, not just labels: worktree rows colored
  by `kind`; repo `dirty`/`ahead`/`behind` warn/heal/dim by state;
  pool & project `status` colored via `pm-faces-status`
  (FREE/OPS/DRIFT/…); session IDs as `pm-id`, timestamps dimmed, agent
  names colored.
- **Empty sections** show a dim `(none)` instead of a bare header.

All faces live in `pm-faces.el` and inherit from built-ins / magit
faces where possible — override any of them with `M-x customize-group
RET pm-faces` or a `set-face-attribute` in your config.

#### `*pm-status: <name>*` (`M-x pm-project-status`)

Sections: **Worktrees** (grouped by repo), **PRs**, **Stacker** (rich
markup parsed into Emacs faces), **Recent sessions** (grouped by
project, agent name colored).

| Key   | Action                                                       |
| ----- | ------------------------------------------------------------ |
| `g`   | Refetch every section (`pm project status --json`)           |
| `r w` | Refetch only Worktrees (`-s worktrees`)                      |
| `r p` | Refetch only PRs (`-s prs`)                                  |
| `r s` | Refetch only Stacker (`-s stacker`)                          |
| `r a` | Refetch only Sessions (`-s sessions`)                        |
| `RET` | Action depends on row: switch project (worktree), open URL (PR), resume (session) |
| `q`   | Quit                                                         |

Per-section refresh exists because `pm project status` accepts a
`--section` flag (see "CLI extensions" below) — Emacs only re-pays for
the section the user wants up-to-date.

#### `*pm-agent-ls*` (`M-x pm-agent-list`)

Sections per project; rows per session, agent name styled. `RET`
hands the row off to `pm-agent-dispatch-function`. Prefix arg prompts
for a project; double-prefix forces `--all`.

The all-projects view uses the bare `*pm-agent-ls*` name; a
single-project view (prefix arg, or `l` from `pm-project-dispatch`)
is suffixed as `*pm-agent-ls: <project>*` so per-project lists get
their own buffer instead of clobbering each other or the all view.

#### Project / pool / repo

`pm-project-list`, `pm-pool-list`, `pm-repo-list` are also
`magit-section` buffers. Common keys: `g` refresh, `RET` row action,
`q` quit. Project-list adds `s` (status) and `D` (delete); repo-list's
`RET` pulls the repo at point.

### Launching and resuming agent sessions from Emacs

Both the fresh-launch transient verbs (`C-x p P` → `C`/`O`/`U`,
`C-x p .` → `c`/`o`/`u`) and `RET` on a session row in `*pm-agent-ls*`
flow through `pm-agent-dispatch-function`. Default is `nil`, which
stages the command on the kill-ring and prints a hint — safe out of
the box.

Three opt-in dispatchers ship with the package:

```elisp
;; Built-in `term-mode' (no extra deps)
(setq pm-agent-dispatch-function #'pm-agent-dispatch-term)

;; vterm (if you have it)
(setq pm-agent-dispatch-function #'pm-agent-dispatch-vterm)

;; ghostel (if you have it)
(setq pm-agent-dispatch-function #'pm-agent-dispatch-ghostel)
```

Or write your own — the function receives a plist with `:argv`,
`:cwd`, `:agent`, `:session-id` (nil for a fresh launch), `:project`.

`M-x pm-agent-launch` is also available standalone — prompts for an
agent and project and runs the same dispatcher.

### CLI extensions

`pm project status` learned `--section` / `-s`:

```sh
pm project status -s worktrees,prs       # skip stacker + sessions
pm project status -s sessions --json     # cheapest variant
```

`-s` accepts a comma-separated subset of
`worktrees,prs,stacker,sessions`. JSON output only includes keys for
the requested sections; unknown values fail with `pm: unknown section
'<name>'`.

### Subproject naming

`project-name` is advised so any `vc/Git` project rooted under
`pm-projects-dir/<container>/<alias>/` (or its resolved slot path)
displays as `<container>/<alias>`. The modeline,
`consult-project-buffer`, and any other consumer of `project-name`
pick this up automatically. Containers themselves still display as
just `<container>`.

The advice uses the worktree alias from the symlink path (the name
you gave with `--wt mywt:repo`), not the underlying repo name.

### Federation

When you switch to a container (e.g. `C-x p p emacs-daemon`),
`project-find-file` enumerates files across every attached worktree,
prefixed by alias (`emacs/init.el`, `kms/cmd/main.go`, ...). Set
`pm-include-files-from-worktrees` to nil to fall back to listing only
top-level container entries.

## Configuration

| Custom                              | Default                           | Purpose                                  |
| ----------------------------------- | --------------------------------- | ---------------------------------------- |
| `pm-executable`                     | `"pm"`                            | Resolved via `executable-find` on first use. |
| `pm-projects-dir`                   | `"~/.projects/"`                  | Container root.                          |
| `pm-confirm-destructive`            | `t`                               | Prompt before destructive ops.           |
| `pm-include-files-from-worktrees`   | `t`                               | Federate `project-files` across worktrees. |
| `pm-agent-dispatch-function`        | `nil`                             | Function called on `RET` over a session row. `nil` stages the command on the kill-ring; set to `pm-agent-dispatch-term`, `pm-agent-dispatch-vterm`, or your own to launch in-Emacs. |

## Tests

```sh
emacs -Q -batch -L . -l ert -l pm-tests.el -f ert-run-tests-batch-and-exit
```

All tests are hermetic — `pm` is not invoked.

## Notes on `twist.nix`

A `:load-path` `use-package` is invisible to twist's package
autodetection (no MELPA recipe needed). If you want the package built
into a nix-pinned Emacs, add a recipe at `recipes/pm` pointing at the
local source path; otherwise the `:load-path` form just works.

## Stacker

`pm-stacker.el` provides an interactive, multi-repo view of the stacker
plus the full action surface. Open it with `M-x pm-stacker-list`, via
`C-x p .` → `k`, or `C-x p P` → `k`. The same tree also renders as the
**Stacker** section of `*pm-status:*` (RET on a branch there opens the
same action menu).

Both surfaces consume the structured tree that `pm project status
--json` now emits (`StackerRow.branches` / `current_branch` /
`operation`); the dedicated buffer fetches it in one call via
`pm project status -s worktrees,stacker --json`.

### `*pm-stacker: <project>*`

One foldable branch tree per tracked repo. Each row shows the status
icon (mirroring the CLI: `· ○ ● ▼ ✓ ⚑ ◼ ■`), the branch name (the
current branch bold, merged branches struck through), and commit-group
markers (`(N)` commits, `!` needs-sync, `↑N` ahead of remote). A
paused/conflicted operation shows a `⚠` banner under its repo.

Movement / folding (`n`/`p`/`TAB`/`S-TAB`) are inherited from
`magit-section`.

| Key   | Action                                                      |
| ----- | ----------------------------------------------------------- |
| `RET` | Open the branch-scoped action transient                     |
| `w`   | Visit the branch's worktree (falls back to its PR)          |
| `b`   | Browse the branch's PR                                       |
| `L`   | `pm stacker log` for the branch                             |
| `p` / `y` / `a` | push / sync / absorb                              |
| `c` / `r` / `R` / `S` / `x` | create / rename / reparent / split / remove |
| `C` / `A` / `F` | continue / abort / repair                         |
| `P` / `U` | pr refresh / pr unlink                                  |
| `g`   | Refresh                                                     |
| `q`   | Quit                                                        |

### Action transient

`RET` on a branch opens `pm-stacker-dispatch`, scoped to that branch.
Infix arguments (`--all`, `--draft`/`--publish`, `--offline`,
`--allow-drop-*`, `--parent`/`--keep-branch`) drive `push` / `sync` /
`remove`; suffix descriptions live-update as you toggle them. The
prompt-driven verbs (create / rename / reparent / split / repair) and
the operation verbs (continue / abort) act on the same branch.

Every action runs `pm stacker <verb> --json` and refreshes the buffer
on completion. Because the JSON envelope carries the post-command
`operation` state, a sync/absorb/reparent that pauses on a conflict
re-renders with the `⚠` banner so `C` (continue) / `A` (abort) are one
key away. Destructive verbs (remove, reparent, split, repair, pr
unlink) confirm first when `pm-confirm-destructive` is set.

### CLI: `--json` on every stacker verb

Every `pm stacker` subcommand — not just `ls`/`config` — accepts
`--json`. Mutating verbs emit a uniform envelope:

```json
{"ok": true, "command": "sync", "repo": "kms", "branch": "feat",
 "message": "<rich-markup result>", "operation": null}
```

`message` is the human result (rendered from Rich markup by the Emacs
client); `operation` is the repo's paused-op state after the command
(or `null`). Without `--json` the human output is unchanged.
