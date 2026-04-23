from __future__ import annotations

import argparse

from . import _common


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("log", help="show commits since the branch's managed base")
    _common.add_repo_branch(p)
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    return _common.run(lambda svc: svc.log_text(_common.target(args, svc.paths)))
