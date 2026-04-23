from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from project_manager import config
from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod

from . import git, ops_slot, selectors
from .db import StackerDB
from .models import OperationState, SelectorTarget
from .service import StackerService


def _service(paths: Paths) -> StackerService:
    db = StackerDB(paths.stacker_db())
    return StackerService(db, paths)


def _target(args: argparse.Namespace) -> SelectorTarget:
    return SelectorTarget(repo_name=args.repo, branch=args.branch)


def _require_base(base: str | None) -> str:
    if not base:
        raise git.GitError("pm stacker create requires --base <parent-branch>.")
    return base


def _cmd_create(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _service(paths)
    try:
        base = _require_base(args.base)
        parent = selectors.resolve_parent_for_base(paths, args.repo, base)
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
    return _run(lambda svc: svc.untrack(_target(args)))


def _cmd_sync(args: argparse.Namespace) -> int:
    return _run(lambda svc: svc.sync(_target(args)))


def _cmd_push(args: argparse.Namespace) -> int:
    return _run(lambda svc: svc.push(_target(args)))


def _cmd_pr(args: argparse.Namespace) -> int:
    return _run(lambda svc: svc.pr(_target(args), draft=args.draft))


def _cmd_status(args: argparse.Namespace) -> int:
    return _run(lambda svc: svc.status_text(_target(args)))


def _cmd_log(args: argparse.Namespace) -> int:
    return _run(lambda svc: svc.log_text(_target(args)))


def _cmd_graph(args: argparse.Namespace) -> int:
    return _run(lambda svc: svc.graph_text(args.repo))


def _cmd_continue(args: argparse.Namespace) -> int:
    return _run(lambda svc: svc.continue_operation(args.repo))


def _cmd_abort(args: argparse.Namespace) -> int:
    return _run(lambda svc: svc.abort_operation(args.repo))


def _cmd_repo_set_pr_mode(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _service(paths)
    try:
        row = service.set_pr_mode(
            repo_name=args.repo,
            mode=args.mode,
            trunk_branch=args.trunk,
            main_repo=args.main_repo,
        )
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    print(f"{row.repo_name}\t{row.mode}\t{row.trunk_branch}")
    return 0


def _cmd_repo_show_pr_mode(args: argparse.Namespace) -> int:
    return _run(lambda svc: svc.show_pr_mode(args.repo))


def _run(op: Callable[[StackerService], str]) -> int:
    paths = config.load()
    try:
        result = op(_service(paths))
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    print(result)
    return 0


_Handler = Callable[[argparse.Namespace], int]


def _add_branch_subcommand(
    sub: argparse._SubParsersAction,
    name: str,
    handler: _Handler,
    *,
    help_text: str,
) -> argparse.ArgumentParser:
    p = sub.add_parser(name, help=help_text)
    p.add_argument("--repo", required=True)
    p.add_argument("branch")
    p.set_defaults(func=handler)
    return p


def _add_stacker_commands(sub: argparse._SubParsersAction) -> None:
    create = sub.add_parser("create", help="claim a slot, create branch off --base, track it")
    create.add_argument("--repo", required=True)
    create.add_argument("-b", "--branch", required=True)
    create.add_argument("--base", help="parent branch (or repo:parent)")
    create.set_defaults(func=_cmd_create)

    track = _add_branch_subcommand(
        sub, "track", _cmd_track, help_text="adopt an existing branch into the stack"
    )
    track.add_argument("--parent", required=True)

    _add_branch_subcommand(
        sub, "untrack", _cmd_untrack, help_text="stop tracking a branch; reparent children"
    )
    _add_branch_subcommand(
        sub, "sync", _cmd_sync, help_text="cherry-pick the branch onto its parent's tip"
    )
    _add_branch_subcommand(
        sub, "push", _cmd_push, help_text="sync every descendant of this branch",
    )
    pr = _add_branch_subcommand(sub, "pr", _cmd_pr, help_text="pp + create/update PR")
    pr.add_argument("--draft", action="store_true")
    _add_branch_subcommand(
        sub, "status", _cmd_status, help_text="show tracking + operation state",
    )
    _add_branch_subcommand(
        sub, "log", _cmd_log, help_text="show commits since the branch's managed base",
    )


def _add_repo_only_commands(sub: argparse._SubParsersAction) -> None:
    graph = sub.add_parser("graph", help="render tracked branches as a tree")
    graph.add_argument("--repo", default=None)
    graph.set_defaults(func=_cmd_graph)

    for name, handler, help_text in (
        ("continue", _cmd_continue, "resume a paused stacker operation"),
        ("abort", _cmd_abort, "abort a paused stacker operation and reset state"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--repo", required=True)
        p.set_defaults(func=handler)


def _add_repo_config_commands(sub: argparse._SubParsersAction) -> None:
    repo = sub.add_parser("repo", help="per-repo PR configuration")
    repo_sub = repo.add_subparsers(dest="repo_cmd", required=True)

    set_pr = repo_sub.add_parser("set-pr-mode", help="configure PR mode")
    set_pr.add_argument("--repo", required=True)
    set_pr.add_argument("--mode", choices=["normal", "forked"], required=True)
    set_pr.add_argument("--trunk", required=True)
    set_pr.add_argument("--main-repo", default=None)
    set_pr.set_defaults(func=_cmd_repo_set_pr_mode)

    show_pr = repo_sub.add_parser("show-pr-mode", help="show PR mode")
    show_pr.add_argument("--repo", required=True)
    show_pr.set_defaults(func=_cmd_repo_show_pr_mode)


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    stacker = subparsers.add_parser(
        "stacker", help="branch-stack tracking against pm's pool"
    )
    sub = stacker.add_subparsers(dest="cmd", required=True)
    _add_stacker_commands(sub)
    _add_repo_only_commands(sub)
    _add_repo_config_commands(sub)


def gc_ops(paths: Paths) -> list[slot_mod.Slot]:
    """Release stacker-ops slots whose repo has no active operation row.

    Safe to run at any time: a slot is only released if it is (a) claimed by
    the stacker ops marker and (b) has no matching `operations` row in the
    stacker db pointing at its current branch.
    """
    if not paths.stacker_root.is_dir() or not paths.worktrees.is_dir():
        return []
    db = StackerDB(paths.stacker_db())
    ops_by_repo = _ops_by_repo(db.list_operations())
    marker = paths.stacker_ops_marker().resolve()
    released: list[slot_mod.Slot] = []
    for repo_dir in sorted(paths.worktrees.iterdir()):
        if not repo_dir.is_dir():
            continue
        released.extend(
            _collect_orphan_ops_slots(
                paths, repo_dir.name, ops_by_repo.get(repo_dir.name, set()), marker
            )
        )
    return released


def _ops_by_repo(ops: list[OperationState]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for op in ops:
        out.setdefault(op.repo_name, set())
        if op.branch:
            out[op.repo_name].add(op.branch)
    return out


def _collect_orphan_ops_slots(
    paths: Paths, repo_name: str, live_branches: set[str], marker: Path
) -> list[slot_mod.Slot]:
    released: list[slot_mod.Slot] = []
    for slot in slot_mod.list_slots(paths, repo_name):
        if not _points_at_marker(slot, marker):
            continue
        branch = _slot_branch(slot.path)
        if branch and branch in live_branches:
            continue
        slot_mod.release(slot)
        released.append(slot)
    return released


def _points_at_marker(slot: slot_mod.Slot, marker: Path) -> bool:
    target = slot.owner_target()
    if target is None:
        return False
    try:
        return target.resolve() == marker
    except OSError:
        return False


def _slot_branch(slot_path: Path) -> str | None:
    try:
        return git.current_branch(slot_path) or None
    except git.GitError:
        return None
