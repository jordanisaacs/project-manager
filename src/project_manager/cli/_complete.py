"""Hidden `pm __complete` sub-app — value providers for shell completion.

Each verb prints one candidate per line to stdout and exits 0. No stderr,
no exceptions: completion runs on every keypress and must never disturb
the user's prompt.

Stdout contract (internal but stable — `integrations/_pm` parses it):
    <value>            # bare
    <value>:<desc>     # with zsh short-description

This module exists because cyclopts (as of v4.10) has no hook for dynamic
per-parameter completion. See upstream issue #641. When that lands, the
bodies below collapse into plain `() -> list[str]` functions attached via
`Parameter(completion=...)` and this sub-app can be deleted.
"""

import re
from contextlib import suppress
from typing import Literal

from cyclopts import App

from project_manager import config
from project_manager.cli._params import ProjectArg
from project_manager.cli._shared import root
from project_manager.project import current, discovery
from project_manager.project import db as project_db
from project_manager.repo import ls as repo_ls

complete_app = root.command(
    App(name="__complete", show=False, help=""),
)


@complete_app.command(show=False)
def projects() -> int:
    """Project names under `paths.projects`."""
    with suppress(Exception):
        paths = config.load()
        for name, _ in discovery.list_project_dbs(paths):
            print(name)
    return 0


@complete_app.command(show=False)
def repos() -> int:
    """Canonical repo names under `paths.repos`."""
    with suppress(Exception):
        paths = config.load()
        for row in repo_ls.ls(paths):
            print(row.repo)
    return 0


@complete_app.command(show=False)
def worktrees(
    *,
    project: ProjectArg = None,
    state: Literal["all", "attached", "unattached"] = "all",
) -> int:
    """Worktree names for a project, optionally filtered by attach state.

    --state attached   → rows that currently have a forward symlink.
    --state unattached → rows without a forward symlink.
    --state all        → every row (default).
    """
    with suppress(Exception):
        paths = config.load()
        resolved = current.resolve_project(paths, project)
        db_path = discovery.require_project_db(paths, resolved)
        with project_db.readonly(db_path) as conn:
            rows = project_db.list_wts(conn)
        for wt, _, _ in rows:
            if state == "all":
                print(wt)
                continue
            attached = paths.forward(resolved, wt).is_symlink()
            if (state == "attached" and attached) or (state == "unattached" and not attached):
                print(wt)
    return 0


# --- zsh completion script generator ----------------------------------------
#
# Cyclopts's `App.generate_completion(shell="zsh")` emits a correct nested
# state-machine skeleton but leaves every positional slot opaque (`'1:--repo'`,
# etc. with no action). We post-process it to bind specific slots to the
# dynamic helpers below, and append those helpers to the bottom of the
# script.
#
# Consumed by `pm __complete zsh-script > ~/.zfunc/_pm`.

_HELPERS = r"""
# --- pm dynamic completion helpers (appended by pm __complete zsh-script) ---
# Cyclopts's generated script ends with `}` and no self-invocation. Under
# zsh-style autoload that means the first TAB only redefines _pm; the user
# would need a second TAB to trigger completion. Invoking _pm at the end
# of the file body (so on first load it runs once) fixes that.

_pm_extract_flag() {
  # _pm_extract_flag --flag [--alias ...]
  # Scan $words for the first matching flag; echo the value immediately
  # after it, or nothing.
  local flag i=2
  while (( i < ${#words} )); do
    for flag; do
      [[ $words[i] == $flag ]] && { print -r -- "$words[i+1]"; return 0; }
    done
    (( i++ ))
  done
  return 1
}

_pm_projects() {
  local -a items
  items=(${(f)"$(command pm __complete projects 2>/dev/null)"})
  _describe -t projects 'project' items
}

_pm_repos() {
  local -a items
  items=(${(f)"$(command pm __complete repos 2>/dev/null)"})
  _describe -t repos 'repo' items
}

_pm_wt_list() {
  # Comma-list completion for --wt. `_values -s ,` handles subtraction
  # of already-typed values automatically. $1 selects the state filter
  # (all | attached | unattached).
  local state=${1:-all}
  local project=$(_pm_extract_flag --project -p)
  local -a args items
  (( ${#project} )) && args=(--project "$project")
  items=(${(f)"$(command pm __complete worktrees --state $state $args 2>/dev/null)"})
  if (( ${#items} == 0 )); then
    # `_values` errors with "not enough arguments" if no candidates follow
    # the description; surface an inline hint instead of a hard error.
    _message -e worktrees "no $state worktrees for this project"
    return 1
  fi
  _values -s , 'worktree' $items
}
_pm_wt_attachable() { _pm_wt_list unattached }
_pm_wt_detachable() { _pm_wt_list attached }
_pm_wt_all()        { _pm_wt_list all }

_pm_wt_for_cd() {
  # `pm cd <project> <wt>` — read the project from the first positional
  # ($words[1]=pm, $words[2]=cd, $words[3]=<project>). Falls back to the
  # --project flag if someone wrote it that way.
  local project=${words[3]:-}
  if [[ -z "$project" || "$project" == --* || "$project" == -* ]]; then
    project=$(_pm_extract_flag --project -p)
  fi
  if [[ -z "$project" ]]; then
    _message -e worktrees "specify a project first"
    return 1
  fi
  local -a items
  items=(${(f)"$(command pm __complete worktrees --state all --project "$project" 2>/dev/null)"})
  if (( ${#items} == 0 )); then
    _message -e worktrees "no worktrees in project '$project'"
    return 1
  fi
  _describe -t worktrees 'worktree' items
}

_pm "$@"
"""


# Unconditional substitutions — applied everywhere the needle appears.
# Each entry must match at least once; a miss prints `# WARN:` so cyclopts
# output drift is visible on install.
_SIMPLE_SUBS: tuple[tuple[str, str], ...] = (
    # --repo (pool add/ls + every stacker command with `--repo`)
    ("'--repo[--repo]:repo'", "'--repo[--repo]:repo:_pm_repos'"),
    (
        "'--repo[repo name (defaults to the cwd'\\''s pm slot)]:repo'",
        "'--repo[repo name (defaults to the cwd'\\''s pm slot)]:repo:_pm_repos'",
    ),
    # --project / -p flag forms (every wt subcommand uses the descriptive
    # form; project delete/status use the terse form — both get bound).
    (
        "'--project[project name (defaults to current project)]:project'",
        "'--project[project name (defaults to current project)]:project:_pm_projects'",
    ),
    (
        "'-p[project name (defaults to current project)]:p'",
        "'-p[project name (defaults to current project)]:p:_pm_projects'",
    ),
    # `pm pool {ls,add}` positional → repos.
    ("'1:--repo'", "'1:repo:_pm_repos'"),
    # `pm project wt add` positional spec defaults to `<repo>` (or
    # `<wt>:<repo>`). Completing repo names covers the common case.
    ("'1:--spec'", "'1:repo:_pm_repos'"),
    # `pm cd <project> <wt>` — `'2:--wt'` only appears here, and the
    # `'--wt[--wt]:wt'` in `pm project create` (a comma-separated *spec*,
    # not an existing wt name) is shielded by the create-block mask
    # below, so a global rewrite is safe.
    ("'2:--wt'", "'2:wt:_pm_wt_for_cd'"),
    ("'--wt[--wt]:wt'", "'--wt[--wt]:wt:_pm_wt_for_cd'"),
)

# --- Context-sensitive substitutions ----------------------------------------
#
# Some slot strings are shared across subcommands with different semantics:
#
#   - `pm project create` has `'1:--project'` and `'--project[--project]:project'`
#     — both represent a NEW project name; must NOT complete.
#   - `pm project {delete,status}` use the same strings — DO complete.
#   - `pm project wt {attach,detach,remove}` all use
#     `'--wt[comma-separated worktree names]:wt'` but need different helpers
#     (attachable / detachable / all).
#
# Approach: mask offending blocks before global substitution, then do
# per-subcommand rewrites for the `--wt` slot inside the wt section.

# Matches the project-level `create)` _arguments block (from the `create)`
# label through the next `;;`). `'1:--spec'` only appears in wt-add, so
# this pattern anchors on the `'1:--project'` line which is unique to the
# project-level create.
_PROJECT_CREATE_BLOCK = re.compile(
    r"create\)\n\s+_arguments \\\n\s+'1:--project'.*?;;",
    re.DOTALL,
)

_WT_FLAG_BY_SUB: dict[str, str] = {
    "attach": "_pm_wt_attachable",
    "detach": "_pm_wt_detachable",
    "remove": "_pm_wt_all",
}

_WT_NEEDLE = "'--wt[comma-separated worktree names]:wt'"


def _rewrite_wt_flag_per_sub(script: str, *, warn: list[str]) -> str:
    """Bind --wt to a different helper per wt subcommand (attach/detach/remove).

    Walks each `<sub>) _arguments ... ;;` block in turn and substitutes the
    `--wt` slot inside. Blocks that don't contain the slot (e.g. `delete)`
    at the project level, or stacker commands) are touched but unchanged.
    """
    for sub, helper in _WT_FLAG_BY_SUB.items():
        replacement = f"{_WT_NEEDLE[:-1]}:{helper}'"  # splice helper before closing quote
        block_re = re.compile(rf"({sub}\)\n\s+_arguments \\\n.*?;;)", re.DOTALL)
        script = block_re.sub(
            lambda m, r=replacement: m.group(1).replace(_WT_NEEDLE, r),
            script,
        )
    if _WT_NEEDLE in script:
        warn.append(f"--wt slot still present after per-sub rewrite: {_WT_NEEDLE}")
    return script


@complete_app.command(name="zsh-script", show=False)
def zsh_script() -> int:
    """Print the full zsh completion script (generator + dynamic hooks)."""
    script = root.generate_completion(shell="zsh", prog_name="pm")  # noqa: S604 (not a subprocess shell arg)
    warn: list[str] = []

    # 1. Mask the project-level `create)` block so its `'1:--project'` and
    # `'--project[--project]:project'` lines don't get rewritten — that
    # positional is the NEW project name, we must not complete it.
    stash: str | None = None
    m = _PROJECT_CREATE_BLOCK.search(script)
    if m is None:
        warn.append(
            "project-level `create)` block not found; --project autocompletion may leak into it",
        )
    else:
        stash = m.group(0)
        script = script.replace(stash, "\0CREATE_MASK\0", 1)

    # 2. Global unconditional substitutions.
    for needle, replacement in _SIMPLE_SUBS:
        if needle not in script:
            warn.append(f"substitution not applied: {needle}")
            continue
        script = script.replace(needle, replacement)

    # 3. `pm project {delete,status}` positional — apply only now, with the
    #    create block masked.
    script = script.replace("'1:--project'", "'1:project:_pm_projects'")
    # The terse `--project[--project]:project` form only appears in
    # delete/status too (wt subs use the descriptive text).
    script = script.replace(
        "'--project[--project]:project'",
        "'--project[--project]:project:_pm_projects'",
    )

    # 4. Per-sub --wt binding.
    script = _rewrite_wt_flag_per_sub(script, warn=warn)

    # 5. Restore the masked project-create block unchanged.
    if stash is not None:
        script = script.replace("\0CREATE_MASK\0", stash, 1)

    for w in warn:
        print(f"# WARN: {w}")
    print(script)
    print(_HELPERS)
    return 0
