# Block dangerous Git flows on pm-stacker-managed branches in interactive shells.
#
# Source this file from ~/.zshrc:
#   [[ -f /path/to/pm-git-guard.zsh ]] && source /path/to/pm-git-guard.zsh
#
# It shadows `git` and, for `git pull` / `git rebase`, asks
# `pm stacker guard no-rebase` whether the current branch is tracked by pm
# stacker. If so, the command is blocked with a guidance message.

if [[ -n "${__PM_GIT_GUARD_LOADED:-}" ]]; then
  return 0
fi
typeset -g __PM_GIT_GUARD_LOADED=1

__pm_git_guard_parse() {
  typeset -g __pm_git_guard_cwd="$PWD"
  typeset -g __pm_git_guard_subcommand=""

  local cwd="$PWD"
  local i=1
  local arg next

  while (( i <= $# )); do
    arg="${@[i]}"
    case "$arg" in
      -C)
        if (( i == $# )); then
          break
        fi
        next="${@[i+1]}"
        if [[ "$next" == /* ]]; then
          cwd="$next"
        else
          cwd="$cwd/$next"
        fi
        cwd="${cwd:A}"
        (( i += 2 ))
        ;;
      -c|--git-dir|--work-tree|--namespace|--super-prefix|--config-env)
        (( i += 2 ))
        ;;
      --exec-path=*|--git-dir=*|--work-tree=*|--namespace=*|--super-prefix=*|--config-env=*|-c*)
        (( i += 1 ))
        ;;
      --paginate|--no-pager|--no-replace-objects|--bare)
        (( i += 1 ))
        ;;
      --)
        (( i += 1 ))
        if (( i <= $# )); then
          typeset -g __pm_git_guard_subcommand="${@[i]}"
        fi
        break
        ;;
      -*)
        (( i += 1 ))
        ;;
      *)
        typeset -g __pm_git_guard_subcommand="$arg"
        break
        ;;
    esac
  done

  typeset -g __pm_git_guard_cwd="$cwd"
}

__pm_git_guard_should_block() {
  case "$1" in
    pull|rebase)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

__pm_git_guard_check() {
  local guard_cwd="$1"

  command -v pm >/dev/null 2>&1 || return 0

  local output
  output="$(
    cd "$guard_cwd" 2>/dev/null || exit 0
    command git rev-parse --show-toplevel >/dev/null 2>&1 || exit 0
    command pm stacker guard no-rebase 2>&1
  )"
  local guard_status=$?

  if (( guard_status == 0 )); then
    return 0
  fi

  [[ -n "$output" ]] && print -u2 -- "$output"
  return $guard_status
}

git() {
  __pm_git_guard_parse "$@"
  if __pm_git_guard_should_block "$__pm_git_guard_subcommand"; then
    __pm_git_guard_check "$__pm_git_guard_cwd" || return $?
  fi
  command git "$@"
}
