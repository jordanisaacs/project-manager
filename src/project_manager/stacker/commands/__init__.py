"""One-file-per-command argparse wiring for `pm stacker`."""
from __future__ import annotations

import argparse

from . import (
    abort,
    config,
    create,
    guard,
    log,
    ls,
    push,
    remove,
    rename,
    reparent,
    split,
    sync,
)
from . import (
    continue_ as continue_cmd,
)


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    stacker = subparsers.add_parser(
        "stacker", help="branch-stack tracking against pm's pool"
    )
    sub = stacker.add_subparsers(dest="cmd", required=True)
    create.add(sub)
    sync.add(sub)
    push.add(sub)
    ls.add(sub)
    remove.add(sub)
    reparent.add(sub)
    split.add(sub)
    rename.add(sub)
    log.add(sub)
    continue_cmd.add(sub)
    abort.add(sub)
    config.add(sub)
    guard.add(sub)
