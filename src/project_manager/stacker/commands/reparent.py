from __future__ import annotations

import argparse
import sys

from project_manager import config
from project_manager.stacker import git

from . import _common


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "reparent",
        help="move current branch onto a new parent; cherry-pick descendants",
    )
    p.add_argument("--repo", default=None, help="defaults to the cwd's pm slot")
    p.add_argument(
        "new_parent", nargs="?", default=None,
        help="new parent (keyword or branch name)",
    )
    p.add_argument(
        "--branch", dest="branch", default=None,
        help="branch to reparent (defaults to cwd's current branch)",
    )
    p.add_argument(
        "--continue", action="store_true", dest="continue_",
        help="resume a paused cherry-pick",
    )
    p.add_argument("--abort", action="store_true", help="abort the paused op")
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
    if not args.new_parent:
        print("pm: reparent requires <new-parent> (or --continue / --abort).",
              file=sys.stderr)
        return 2
    try:
        target = _common.target(args, paths)
        new_parent = _common.resolve_on_spec(paths, target.repo_name, args.new_parent)
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return _common.run(lambda svc: svc.reparent(target, new_parent))
