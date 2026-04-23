from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from project_manager import config
from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OwnerKind, PoolDB

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
    pooldb = PoolDB(paths.pool_db())
    try:
        base = _require_base(args.base)
        parent = selectors.resolve_parent_for_base(paths, args.repo, base)
        target_slot = ops_slot.claim(
            paths,
            pooldb,
            args.repo,
            wait=ops_slot.WaitOptions(progress=_stderr_progress),
        )
        try:
            service.initialize_worktree(
                repo_name=args.repo,
                worktree_path=target_slot.path,
                branch=args.branch,
                create_branch=True,
                parent=parent,
            )
        except Exception:
            pooldb.release(target_slot.repo, target_slot.uuid)
            raise
    except (git.GitError, slot_mod.PoolExhaustedError) as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    print(target_slot.path)
    return 0


def _stderr_progress(msg: str) -> None:
    print(f"pm: {msg}", file=sys.stderr)


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


def _cmd_config(args: argparse.Namespace) -> int:
    service = _service(config.load())
    try:
        return _dispatch_config(service, args)
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2


def _dispatch_config(service: StackerService, args: argparse.Namespace) -> int:
    if args.list:
        return _config_list(service, args)
    if args.unset:
        return _config_unset(service, args)
    if args.key is None:
        print("pm: config requires a key (or --list / --unset).", file=sys.stderr)
        return 2
    if args.value is None:
        return _config_get(service, args)
    return _config_set(service, args)


def _config_list(service: StackerService, args: argparse.Namespace) -> int:
    if args.key is not None or args.value is not None:
        print("pm: --list takes no key/value arguments.", file=sys.stderr)
        return 2
    for key, value in service.list_config(args.repo):
        print(f"{key}={value}")
    return 0


def _config_unset(service: StackerService, args: argparse.Namespace) -> int:
    if args.key is None or args.value is not None:
        print("pm: --unset requires exactly one key.", file=sys.stderr)
        return 2
    return 0 if service.unset_config(args.repo, args.key) else 1


def _config_get(service: StackerService, args: argparse.Namespace) -> int:
    current = service.get_config(args.repo, args.key)
    if current is None:
        return 1
    print(current)
    return 0


def _config_set(service: StackerService, args: argparse.Namespace) -> int:
    for note in service.set_config(args.repo, args.key, args.value):
        print(note, file=sys.stderr)
    return 0


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


def _add_config_command(sub: argparse._SubParsersAction) -> None:
    cfg = sub.add_parser("config", help="get/set per-repo stacker config (git-config style)")
    cfg.add_argument("--repo", required=True)
    group = cfg.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="list all set keys")
    group.add_argument("--unset", action="store_true", help="remove a key")
    cfg.add_argument("key", nargs="?", help="config key (e.g. pr.mode)")
    cfg.add_argument("value", nargs="?", help="value to set; omit to read")
    cfg.set_defaults(func=_cmd_config)


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    stacker = subparsers.add_parser(
        "stacker", help="branch-stack tracking against pm's pool"
    )
    sub = stacker.add_subparsers(dest="cmd", required=True)
    _add_stacker_commands(sub)
    _add_repo_only_commands(sub)
    _add_config_command(sub)


def gc_ops(paths: Paths) -> list[slot_mod.Slot]:
    """Release stacker-ops slots whose repo has no active operation row.

    Safe to run at any time: a slot is only released if it is (a) recorded in
    the pool db as stacker-owned and (b) has no matching `operations` row in
    the stacker db pointing at its current branch.
    """
    if not paths.worktrees.is_dir():
        return []
    pooldb = PoolDB(paths.pool_db())
    db = StackerDB(paths.stacker_db())
    ops_by_repo = _ops_by_repo(db.list_operations())
    released: list[slot_mod.Slot] = []
    for repo, uuid, owner in pooldb.list_owned():
        if owner.kind != OwnerKind.STACKER:
            continue
        slot_path = paths.slot(repo, uuid)
        if not slot_path.is_dir():
            pooldb.release(repo, uuid)
            released.append(slot_mod.Slot(repo=repo, uuid=uuid, path=slot_path))
            continue
        branch = _slot_branch(slot_path)
        if branch and branch in ops_by_repo.get(repo, set()):
            continue
        pooldb.release(repo, uuid)
        released.append(slot_mod.Slot(repo=repo, uuid=uuid, path=slot_path))
    return released


def _ops_by_repo(ops: list[OperationState]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for op in ops:
        out.setdefault(op.repo_name, set())
        if op.branch:
            out[op.repo_name].add(op.branch)
    return out


def _slot_branch(slot_path: Path) -> str | None:
    try:
        return git.current_branch(slot_path) or None
    except git.GitError:
        return None
