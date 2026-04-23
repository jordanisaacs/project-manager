from __future__ import annotations

import argparse
import sys

from project_manager import config
from project_manager.stacker import git

from . import _common


def add(sub: argparse._SubParsersAction) -> None:
    guard = sub.add_parser("guard", help="internal guardrails for Git hooks")
    guard_sub = guard.add_subparsers(dest="guard_cmd", required=True)
    no_rebase = guard_sub.add_parser(
        "no-rebase",
        help="exit nonzero when cwd is a stacker-tracked branch",
    )
    no_rebase.set_defaults(func=run_no_rebase)


def run_no_rebase(args: argparse.Namespace) -> int:
    del args
    paths = config.load()
    service = _common.service(paths)
    try:
        service.guard_no_rebase()
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return 0
