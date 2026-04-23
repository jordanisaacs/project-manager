from __future__ import annotations

import argparse

from . import _common


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "split",
        help="move commits [<commit>..HEAD] onto a new child branch",
    )
    p.add_argument("--repo", default=None, help="defaults to the cwd's pm slot")
    p.add_argument("--branch", dest="branch", default=None,
                   help="source branch (defaults to cwd's current branch)")
    p.add_argument("new_name", help="name for the new child branch")
    p.add_argument("commit", help="split point (included in the moved range)")
    p.add_argument(
        "--stay", action="store_true",
        help="skip claiming a pool slot for the new branch (ref + DB row still land)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    return _common.run(
        lambda svc: svc.split(
            _common.target(args, svc.paths),
            args.new_name,
            args.commit,
            stay=args.stay,
        )
    )
