from __future__ import annotations

import argparse
import sys

from project_manager import config
from project_manager.stacker import git

from . import _common


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("sync", help="cherry-pick branches onto their parents")
    _common.add_repo_branch(p)
    _common.add_scope_flags(p)
    p.add_argument(
        "--continue", action="store_true", dest="continue_",
        help="resume a paused cherry-pick (same as `pm stacker continue`)",
    )
    p.add_argument(
        "--abort", action="store_true",
        help="abort the paused op (same as `pm stacker abort`)",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    paths = config.load()
    if getattr(args, "continue_", False):
        return _common.run(
            lambda svc: svc.continue_operation(_common.resolve_repo(args, svc.paths))
        )
    if getattr(args, "abort", False):
        return _common.run(
            lambda svc: svc.abort_operation(_common.resolve_repo(args, svc.paths))
        )
    try:
        target = _common.target(args, paths)
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return _common.run(lambda svc: svc.sync(target, _common.scope_spec(args)))
