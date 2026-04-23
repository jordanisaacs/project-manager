from __future__ import annotations

import argparse

from . import _common


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "abort", help="abort a paused stacker operation and reset state"
    )
    p.add_argument("--repo", default=None, help="defaults to the cwd's pm slot")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    return _common.run(
        lambda svc: svc.abort_operation(_common.resolve_repo(args, svc.paths))
    )
