from __future__ import annotations

import argparse
import sys

from project_manager import config
from project_manager.stacker import git
from project_manager.stacker.models import Details, Scope

from . import _common

_DETAILS_CHOICES: tuple[Details, ...] = ("none", "status", "status-counts", "all")


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("ls", help="render the stack tree")
    _common.add_repo_branch(p)
    _common.add_scope_flags(p)
    p.add_argument(
        "--details", default="status-counts", choices=_DETAILS_CHOICES,
        help="per-branch detail level (default: status-counts)",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    paths = config.load()
    scope: Scope = _common.scope(args)
    if scope == "current":
        try:
            target = _common.target(args, paths)
        except git.GitError as e:
            print(f"pm: {e}", file=sys.stderr)
            return 2
        return _common.run(
            lambda svc: svc.ls_text(
                target.repo_name,
                target_branch=target.branch,
                scope="current",
                details=args.details,
                json_output=args.json,
            )
        )
    return _common.run(
        lambda svc: svc.ls_text(
            _common.resolve_repo_optional(args, svc.paths),
            scope="all",
            details=args.details,
            json_output=args.json,
        )
    )
