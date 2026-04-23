from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from project_manager import config
from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import PoolDB
from project_manager.stacker import git, locate, ops_slot, selectors
from project_manager.stacker.models import SelectorTarget, WorktreeInit

from . import _common


def add(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "create",
        help="create or adopt a tracked branch (optionally in a pool slot)",
    )
    p.add_argument("--repo", default=None, help="defaults to the cwd's pm slot")
    p.add_argument("branch", help="new branch name (or existing branch, with --replace)")
    p.add_argument(
        "--on", default=None,
        help="parent: keyword 'current'/'parent', or a branch name (default: current)",
    )
    p.add_argument(
        "--copy", default=None,
        help="copy commits from this branch when creating the new one",
    )
    p.add_argument(
        "--replace", action="store_true",
        help="adopt an existing branch instead of creating a new one",
    )
    p.add_argument(
        "--no-checkout", action="store_true",
        help="do not claim a worktree slot; create the branch ref only",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _common.service(paths)
    pooldb = PoolDB(paths.pool_db())
    try:
        repo_name = _common.resolve_repo(args, paths)
        on_spec = _on_spec_for_create(args)
        parent = _common.resolve_on_spec(
            paths, repo_name, on_spec,
            fallback_branch=args.branch if args.replace else None,
        )
        if args.replace:
            target = SelectorTarget(repo_name=repo_name, branch=args.branch)
            tracked = service.track(target, parent)
            print(selectors.selector_for(tracked.repo_name, tracked.branch))
            return 0
        if args.no_checkout:
            service.create_tracked_branch(
                repo_name, args.branch, parent, copy_from=args.copy,
            )
            print(selectors.selector_for(repo_name, args.branch))
            return 0
        worktree_path, cleanup = _resolve_create_slot(paths, pooldb, repo_name)
        try:
            service.init_new_branch(
                WorktreeInit(
                    repo_name=repo_name,
                    worktree_path=worktree_path,
                    branch=args.branch,
                    parent=parent,
                    copy_from=args.copy,
                )
            )
        except Exception:
            cleanup()
            raise
    except (git.GitError, slot_mod.PoolExhaustedError) as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    print(worktree_path)
    return 0


def _on_spec_for_create(args: argparse.Namespace) -> str:
    """Return the resolved --on keyword, erroring when replace needs an explicit value."""
    if args.on:
        return args.on
    if args.replace:
        raise git.GitError("--replace requires --on <parent-branch>.")
    return "current"


def _resolve_create_slot(
    paths: Paths, pooldb: PoolDB, repo_name: str
) -> tuple[Path, Callable[[], None]]:
    """Pick the worktree to create the new branch in.

    Prefers the cwd slot when it's a pm slot for `repo_name`. Falls back
    to claiming a fresh ops slot. Returns (worktree_path, cleanup) —
    cleanup releases anything we claimed if the create fails; no-op when
    we reused an existing slot.
    """
    here = locate.slot_for_cwd(paths)
    if here is not None and here.repo_name == repo_name:
        return here.path, lambda: None
    claimed = ops_slot.claim(
        paths, pooldb, repo_name,
        wait=ops_slot.WaitOptions(progress=_common.stderr_progress),
    )
    return claimed.path, lambda: pooldb.release(claimed.repo, claimed.uuid)
