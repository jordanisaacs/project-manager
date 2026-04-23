from __future__ import annotations

import argparse

from . import _common


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "repair",
        help="reset stored managed-base / last-synced / last-clean-head to match git",
    )
    p.add_argument("--repo", default=None, help="defaults to the cwd's pm slot")
    p.add_argument(
        "--branch", dest="branch", default=None,
        help="branch to repair (defaults to cwd's current branch)",
    )
    p.add_argument(
        "base_ref",
        help="ref whose tip becomes the new managed_base (e.g. master, HEAD~3)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    return _common.run(
        lambda svc: svc.repair(_common.target(args, svc.paths), args.base_ref)
    )
