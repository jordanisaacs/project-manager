from __future__ import annotations

import argparse
import sys
from pathlib import Path

from project_manager import config
from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod

from . import git, ops_slot, selectors
from .db import StackerDB
from .models import SelectorTarget
from .service import StackerService


def _service(paths: Paths) -> StackerService:
    db = StackerDB(paths.stacker_db())
    return StackerService(db, paths)


def _target(args: argparse.Namespace) -> SelectorTarget:
    return SelectorTarget(repo_name=args.repo, branch=args.branch)


def _cmd_create(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _service(paths)
    try:
        if not args.base:
            raise git.GitError("pm stacker create requires --base <parent-branch>.")
        parent = selectors.resolve_parent_for_base(paths, args.repo, args.base)
        target_slot = ops_slot.claim(paths, args.repo)
        try:
            service.initialize_worktree(
                repo_name=args.repo,
                worktree_path=target_slot.path,
                branch=args.branch,
                create_branch=True,
                parent=parent,
            )
        except Exception:
            slot_mod.release(target_slot)
            raise
    except (git.GitError, slot_mod.PoolExhaustedError) as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    print(target_slot.path)
    return 0


def _cmd_track(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _service(paths)
    try:
        parent = selectors.resolve_parent_for_base(paths, args.repo, args.parent)
        tracked = service.track(_target(args), parent)
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    print(selectors.selector_for(tracked.repo_name, tracked.branch))
    return 0


def _cmd_untrack(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _service(paths)
    try:
        message = service.untrack(_target(args))
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    print(message)
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _service(paths)
    try:
        print(service.sync(_target(args)))
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return 0


def _cmd_push(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _service(paths)
    try:
        print(service.push(_target(args)))
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return 0


def _cmd_pr(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _service(paths)
    try:
        print(service.pr(_target(args), draft=args.draft))
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _service(paths)
    try:
        print(service.status_text(_target(args)))
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return 0


def _cmd_log(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _service(paths)
    try:
        print(service.log_text(_target(args)))
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return 0


def _cmd_graph(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _service(paths)
    try:
        print(service.graph_text(args.repo))
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return 0


def _cmd_continue(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _service(paths)
    try:
        print(service.continue_operation(args.repo))
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return 0


def _cmd_abort(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _service(paths)
    try:
        print(service.abort_operation(args.repo))
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return 0


def _cmd_repo_set_pr_mode(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _service(paths)
    try:
        config_row = service.set_pr_mode(
            repo_name=args.repo,
            mode=args.mode,
            trunk_branch=args.trunk,
            main_repo=args.main_repo,
        )
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    print(f"{config_row.repo_name}\t{config_row.mode}\t{config_row.trunk_branch}")
    return 0


def _cmd_repo_show_pr_mode(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _service(paths)
    try:
        print(service.show_pr_mode(args.repo))
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return 0


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    stacker = subparsers.add_parser("stacker", help="branch-stack tracking against pm's pool")
    sub = stacker.add_subparsers(dest="cmd", required=True)

    create = sub.add_parser("create", help="claim a slot, create a branch off --base, track it")
    create.add_argument("--repo", required=True)
    create.add_argument("-b", "--branch", required=True)
    create.add_argument("--base", help="parent branch (or repo:parent)")
    create.set_defaults(func=_cmd_create)

    track = sub.add_parser("track", help="adopt an existing branch into the stack")
    track.add_argument("--repo", required=True)
    track.add_argument("branch")
    track.add_argument("--parent", required=True)
    track.set_defaults(func=_cmd_track)

    untrack = sub.add_parser("untrack", help="stop tracking a branch; reparent children")
    untrack.add_argument("--repo", required=True)
    untrack.add_argument("branch")
    untrack.set_defaults(func=_cmd_untrack)

    sync = sub.add_parser("sync", help="cherry-pick the branch onto its parent's tip")
    sync.add_argument("--repo", required=True)
    sync.add_argument("branch")
    sync.set_defaults(func=_cmd_sync)

    push = sub.add_parser("push", help="sync every descendant of this branch in topo order")
    push.add_argument("--repo", required=True)
    push.add_argument("branch")
    push.set_defaults(func=_cmd_push)

    pr = sub.add_parser("pr", help="pp + create/update PR for the branch")
    pr.add_argument("--repo", required=True)
    pr.add_argument("branch")
    pr.add_argument("--draft", action="store_true")
    pr.set_defaults(func=_cmd_pr)

    status = sub.add_parser("status", help="show tracking + operation state for a branch")
    status.add_argument("--repo", required=True)
    status.add_argument("branch")
    status.set_defaults(func=_cmd_status)

    log = sub.add_parser("log", help="show commits since the branch's managed base")
    log.add_argument("--repo", required=True)
    log.add_argument("branch")
    log.set_defaults(func=_cmd_log)

    graph = sub.add_parser("graph", help="render tracked branches as a tree")
    graph.add_argument("--repo", default=None)
    graph.set_defaults(func=_cmd_graph)

    cont = sub.add_parser("continue", help="resume a paused stacker operation")
    cont.add_argument("--repo", required=True)
    cont.set_defaults(func=_cmd_continue)

    abort = sub.add_parser("abort", help="abort a paused stacker operation and reset state")
    abort.add_argument("--repo", required=True)
    abort.set_defaults(func=_cmd_abort)

    repo = sub.add_parser("repo", help="per-repo PR configuration")
    repo_sub = repo.add_subparsers(dest="repo_cmd", required=True)

    set_pr = repo_sub.add_parser("set-pr-mode", help="configure PR mode for a repo")
    set_pr.add_argument("--repo", required=True)
    set_pr.add_argument("--mode", choices=["normal", "forked"], required=True)
    set_pr.add_argument("--trunk", required=True)
    set_pr.add_argument("--main-repo", default=None)
    set_pr.set_defaults(func=_cmd_repo_set_pr_mode)

    show_pr = repo_sub.add_parser("show-pr-mode", help="show PR mode for a repo")
    show_pr.add_argument("--repo", required=True)
    show_pr.set_defaults(func=_cmd_repo_show_pr_mode)


def gc_ops(paths: Paths) -> list[slot_mod.Slot]:
    """Release stacker-ops slots whose repo has no active operation row.

    Safe to run at any time: a slot is only released if it is (a) claimed by
    the stacker ops marker and (b) has no matching `operations` row in the
    stacker db pointing at its current branch.
    """
    released: list[slot_mod.Slot] = []
    if not paths.stacker_root.is_dir():
        return released
    db = StackerDB(paths.stacker_db())
    ops_by_repo: dict[str, set[str]] = {}
    for op in db.list_operations():
        ops_by_repo.setdefault(op.repo_name, set())
        if op.branch:
            ops_by_repo[op.repo_name].add(op.branch)
    marker = paths.stacker_ops_marker().resolve()
    if not paths.worktrees.is_dir():
        return released
    for repo_dir in sorted(paths.worktrees.iterdir()):
        if not repo_dir.is_dir():
            continue
        repo_name = repo_dir.name
        for slot in slot_mod.list_slots(paths, repo_name):
            target = slot.owner_target()
            if target is None:
                continue
            try:
                if target.resolve() != marker:
                    continue
            except OSError:
                continue
            branch = _slot_branch(slot.path)
            if branch and branch in ops_by_repo.get(repo_name, set()):
                continue
            slot_mod.release(slot)
            released.append(slot)
    return released


def _slot_branch(slot_path: Path) -> str | None:
    try:
        return git.current_branch(str(slot_path)) or None
    except git.GitError:
        return None
