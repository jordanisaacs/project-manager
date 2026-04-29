# Make `pm cd <project> [<wt>]` an actual directory change.
#
# `cd` is a shell builtin — the python program can only print the
# resolved path. This wrapper captures that path and runs `builtin cd`
# in the parent shell.
#
# Source this file from ~/.zshrc:
#   [[ -f /path/to/pm-cd.zsh ]] && source /path/to/pm-cd.zsh
#
# `pm cd --print <project>` opts out: the path is printed to stdout,
# nothing is `cd`-ed. Use this in scripts: `cd "$(pm cd --print foo)"`.

if [[ -n "${__PM_CD_LOADED:-}" ]]; then
  return 0
fi
typeset -g __PM_CD_LOADED=1

pm() {
  if [[ "$1" != "cd" ]]; then
    command pm "$@"
    return $?
  fi

  local arg
  for arg in "$@"; do
    if [[ "$arg" == "--print" ]]; then
      command pm "$@"
      return $?
    fi
  done

  local target
  target=$(command pm "$@") || return $?
  [[ -n "$target" ]] || return 1
  builtin cd -- "$target"
}
