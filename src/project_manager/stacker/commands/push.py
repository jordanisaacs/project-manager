from __future__ import annotations

import argparse

from project_manager.stacker.models import PushOptions

from . import _common


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("push", help="force-push + create/update PRs for a scope")
    _common.add_repo_branch(p)
    _common.add_scope_flags(p)
    p.add_argument(
        "--only", action="store_true",
        help="push only the target branch (no lineage walk)",
    )
    draft_group = p.add_mutually_exclusive_group()
    draft_group.add_argument("--draft", action="store_true", help="every PR draft")
    draft_group.add_argument(
        "--publish", action="store_true", help="every PR published (not draft)",
    )
    p.add_argument(
        "--create-pr", dest="create_pr",
        default=True, type=_common.parse_bool,
        help="create/update PRs (default: true); pass false to only force-push",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    options = PushOptions(
        scope=_common.scope_spec(args, include_only=True),
        draft=args.draft,
        publish=args.publish,
        create_pr=args.create_pr,
    )
    return _common.run(lambda svc: svc.push(_common.target(args, svc.paths), options))
