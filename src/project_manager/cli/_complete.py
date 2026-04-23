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
from contextlib import suppress

from cyclopts import App

from project_manager import config
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
def worktrees(*, project: str | None = None) -> int:
    """Worktree names for a project (defaults to the cwd's project)."""
    with suppress(Exception):
        paths = config.load()
        resolved = current.resolve_project(paths, project)
        db_path = discovery.require_project_db(paths, resolved)
        with project_db.readonly(db_path) as conn:
            for wt, _, _ in project_db.list_wts(conn):
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
  # of already-typed values automatically.
  local project=$(_pm_extract_flag --project -p)
  local -a args items
  (( ${#project} )) && args=(--project "$project")
  items=(${(f)"$(command pm __complete worktrees $args 2>/dev/null)"})
  _values -s , 'worktree' $items
}

_pm "$@"
"""


# Substitutions applied to cyclopts's generated script. Each entry rewrites
# an opaque slot into one that calls a dynamic helper. Deliberately
# narrow — we only touch slots where we provide values; stacker/branch
# completion is out of scope (see plan).
_SUBS: tuple[tuple[str, str], ...] = (
    # --repo (pool add/ls + stacker commands)
    ("'--repo[--repo]:repo'", "'--repo[--repo]:repo:_pm_repos'"),
    (
        "'--repo[defaults to the cwd'\\''s pm slot]:repo'",
        "'--repo[defaults to the cwd'\\''s pm slot]:repo:_pm_repos'",
    ),
    # --project (+ -p alias)
    (
        "'--project[--project]:project'",
        "'--project[--project]:project:_pm_projects'",
    ),
    (
        "'--project[project name (defaults to current project)]:project'",
        "'--project[project name (defaults to current project)]:project:_pm_projects'",
    ),
    (
        "'-p[project name (defaults to current project)]:p'",
        "'-p[project name (defaults to current project)]:p:_pm_projects'",
    ),
    # `--wt` comma-list completion.
    (
        "'--wt[comma-separated worktree names]:wt'",
        "'--wt[comma-separated worktree names]:wt:_pm_wt_list'",
    ),
    # positional slots:
    #   pool ls/add  → '1:--repo'
    #   project delete/status → '1:--project'
    ("'1:--repo'", "'1:repo:_pm_repos'"),
    ("'1:--project'", "'1:project:_pm_projects'"),
)


@complete_app.command(name="zsh-script", show=False)
def zsh_script() -> int:
    """Print the full zsh completion script (generator + dynamic hooks)."""
    script = root.generate_completion(shell="zsh", prog_name="pm")  # noqa: S604 (not a subprocess shell arg)
    for needle, replacement in _SUBS:
        if needle not in script:
            # Keep the generator self-checking: if a slot disappears in a
            # future cyclopts release we want to know on the next install.
            print(f"# WARN: substitution not applied: {needle}")
            continue
        script = script.replace(needle, replacement)
    print(script)
    print(_HELPERS)
    return 0
