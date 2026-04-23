from __future__ import annotations

import argparse

from . import _common


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "remove",
        help="untrack a branch (optionally delete it); reparent children",
    )
    _common.add_repo_branch(p)
    p.add_argument("--force", action="store_true", help="skip confirmation")
    p.add_argument(
        "--parent", action="store_true",
        help="also remove all tracked ancestors up the chain",
    )
    p.add_argument(
        "--keep-branch", action="store_true",
        help="untrack without deleting the git branch (old `untrack` behavior)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    return _common.run(
        lambda svc: svc.remove(
            _common.target(args, svc.paths),
            keep_branch=args.keep_branch,
            parent_cascade=args.parent,
            force=args.force,
        )
    )
