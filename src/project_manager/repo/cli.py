import argparse
import sys

from project_manager import config
from project_manager.repo import ls as ls_mod
from project_manager.repo import pull as pull_mod


def _parse_repos(value: str) -> list[str]:
    return [r.strip() for r in value.split(",") if r.strip()]


def _upstream_str(row: ls_mod.RepoRow) -> str:
    if row.branch is None:
        return "-"
    if not row.has_upstream or row.ahead is None or row.behind is None:
        return "no-upstream"
    if row.ahead == 0 and row.behind == 0:
        return "up-to-date"
    parts: list[str] = []
    if row.ahead:
        parts.append(f"ahead {row.ahead}")
    if row.behind:
        parts.append(f"behind {row.behind}")
    return " ".join(parts)


def _cmd_ls(_: argparse.Namespace) -> int:
    paths = config.load()
    for row in ls_mod.ls(paths):
        branch = row.branch if row.branch is not None else "-"
        status = "dirty" if row.dirty else "clean"
        print(f"{row.repo}\t{branch}\t{status}\t{_upstream_str(row)}")
    return 0


def _cmd_pull(args: argparse.Namespace) -> int:
    paths = config.load()
    repos = _parse_repos(args.repo) if args.repo else None
    try:
        results = pull_mod.pull(paths, repos)
    except ValueError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    any_fail = False
    for r in results:
        print(f"{r.repo}\t{'ok' if r.ok else 'fail'}\t{r.message}")
        if not r.ok:
            any_fail = True
    return 1 if any_fail else 0


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    repo = subparsers.add_parser("repo", help="manage canonical repos under ~/.repos")
    sub = repo.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("ls", help="list canonical repos with branch + upstream status")
    ls.set_defaults(func=_cmd_ls)

    pull = sub.add_parser("pull", help="fetch + ff-only pull each canonical repo")
    pull.add_argument("--repo", default=None, help="comma-separated repo names (default: all)")
    pull.set_defaults(func=_cmd_pull)
