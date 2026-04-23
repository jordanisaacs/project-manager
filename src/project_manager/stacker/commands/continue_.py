from __future__ import annotations

import argparse

from . import _common


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("continue", help="resume a paused stacker operation")
    p.add_argument("--repo", default=None, help="defaults to the cwd's pm slot")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    return _common.run(
        lambda svc: svc.continue_operation(_common.resolve_repo(args, svc.paths))
    )
