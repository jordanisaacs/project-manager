# pm.el — Emacs integration for `pm`

Drives the `pm` CLI from inside Emacs. Recognizes pm containers
(`~/.projects/<name>/`) as first-class `project.el` projects, exposes
two magit-style transients — `pm-dispatch` (global, `C-x p P`) and
`pm-project-dispatch` (current project, `C-x p .`) — and treats every
git worktree under a container as a "subproject" of that container.

## Requirements

- Emacs 28.1 or newer (built-in `transient`, `cl-defmethod`).
- `pm` on `$PATH`.

## Install

The package is shipped from this repo. Add a `use-package` block to your
Emacs config pointing at the directory:

```elisp
(use-package pm
  :load-path "~/.repos/project-manager/integrations/emacs/"
  :commands (pm-dispatch pm-project-dispatch pm-refresh
             pm--project-finder pm-jump-parent pm-jump-sibling)
  :init
  (with-eval-after-load 'project
    (add-hook 'project-find-functions #'pm--project-finder)
    (add-to-list 'project-switch-commands
                 '(pm-project-dispatch "PM" ?P) t))
  (global-set-key (kbd "C-x p P") #'pm-dispatch)
  (global-set-key (kbd "C-x p .") #'pm-project-dispatch)
  (add-hook 'after-init-hook
            (lambda () (run-with-idle-timer 0.5 nil #'pm-refresh))))
```

Adjust `:load-path` if the repo lives elsewhere.

## Usage

### `C-x p P` — global pm

| Group   | Key | Command                                                 |
| ------- | --- | ------------------------------------------------------- |
| Create  | `c` | new project                                             |
|         | `+` | mint a pool slot                                        |
| Inspect | `l` | list projects (tabulated)                               |
|         | `o` | list pool                                               |
|         | `R` | list canonical repos                                    |
| Switch  | `p` | switch to a project                                     |
| System  | `g` | refresh                                                 |
|         | `F` | pull all canonical repos                                |
| Project | `.` | enter `pm-project-dispatch` (only shown in a pm tree)   |

### `C-x p .` — current pm project

Header shows `pm: <container>` (or `<container>/<alias>` from inside a
worktree). If invoked outside any pm tree, prompts for a project and
re-enters bound to it. Use a prefix arg (`C-u C-x p .`) to force a
prompt even when in a pm tree.

| Group     | Key  | Toggle / Command                          |
| --------- | ---- | ----------------------------------------- |
| Arguments | `-A` | `--all` (batch wt ops)                    |
|           | `-n` | `--no-branch` (attach skips branch restore) |
|           | `-d` | `--dry-run` (detach simulation)           |
| Inspect   | `s`  | status                                    |
| Worktrees | `n`  | new                                       |
|           | `a`  | attach                                    |
|           | `D`  | detach                                    |
|           | `X`  | delete worktree                           |
| Navigate  | `p`  | parent (when inside a worktree)           |
|           | `j`  | sibling (when ≥ 1 sibling exists)         |
| Danger    | `K`  | delete project                            |
| System    | `g`  | refresh                                   |
|           | `<`  | back to global pm                         |

The `[Navigate]` group hides at the container level (no parent /
sibling there). After `j` lands you in a sibling worktree, use the
standard `C-x p f` for find-file — there's no dedicated find-file-in-sibling.

Suffix descriptions live-update as you toggle infix args, so e.g. with
`-A` enabled, the `D` line reads `detach (all)` and skips the
worktree prompt.

Every `pm` invocation is async; non-zero exits surface via
`display-warning` and a kept `*pm: <verb>*` buffer for inspection.

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

Stacker commands are deliberately omitted. Run them from the shell
(`pm stacker ...`) or send a PR adding a `pm-stacker.el` module.
