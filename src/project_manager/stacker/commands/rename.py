from __future__ import annotations

import argparse

from . import _common


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("rename", help="rename the current branch")
    p.add_argument("--repo", default=None, help="defaults to the cwd's pm slot")
    p.add_argument("--branch", dest="branch", default=None,
                   help="branch to rename (defaults to cwd's current branch)")
    p.add_argument("new_name", help="new branch name")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    return _common.run(
        lambda svc: svc.rename(_common.target(args, svc.paths), args.new_name)
    )
