import argparse
import sys

from project_manager import config
from project_manager.pool import add as add_mod
from project_manager.pool import ls as ls_mod
from project_manager.pool import worktree as wt
from project_manager.stacker import cli as stacker_cli


def _cmd_add(args: argparse.Namespace) -> int:
    paths = config.load()
    try:
        slot = add_mod.add(paths, args.repo)
    except wt.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    print(f"{slot.repo}\t{slot.uuid}\t{slot.path}")
    return 0


def _cmd_ls(args: argparse.Namespace) -> int:
    paths = config.load()
    rows = ls_mod.ls(paths, args.repo)
    for row in rows:
        print(f"{row.repo}\t{row.uuid}\t{row.status}")
    return 0


def _cmd_gc_ops(_args: argparse.Namespace) -> int:
    paths = config.load()
    released = stacker_cli.gc_ops(paths)
    for slot in released:
        print(f"{slot.repo}\t{slot.uuid}\t{slot.path}")
    return 0


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    pool = subparsers.add_parser("pool", help="manage worktree pool")
    sub = pool.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("ls", help="list pool slots with claim status")
    ls.add_argument("repo", nargs="?", default=None)
    ls.set_defaults(func=_cmd_ls)

    add = sub.add_parser("add", help="mint a new pool slot")
    add.add_argument("repo")
    add.set_defaults(func=_cmd_add)

    gc_ops = sub.add_parser(
        "gc-ops", help="release stacker-ops slots that have no live operation"
    )
    gc_ops.set_defaults(func=_cmd_gc_ops)
