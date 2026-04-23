from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from project_manager import config
from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.pool.db import OwnerKind, PoolDB

from . import git, locate, ops_slot, selectors
from .db import StackerDB
from .models import (
    Details,
    OperationState,
    ParentLocator,
    PushOptions,
    Scope,
    ScopeSpec,
    SelectorTarget,
    WorktreeInit,
)
from .service import StackerService


def _service(paths: Paths) -> StackerService:
    db = StackerDB(paths.stacker_db())
    return StackerService(db, paths, progress=_stderr_progress)


def _stderr_progress(msg: str) -> None:
    print(f"pm: {msg}", file=sys.stderr)


def _resolve_repo(args: argparse.Namespace, paths: Paths) -> str:
    if getattr(args, "repo", None):
        return args.repo
    here = locate.slot_for_cwd(paths)
    if here is None:
        raise git.GitError(
            "pass --repo <name> (or run from inside a pm worktree slot)."
        )
    return here.repo_name


def _resolve_repo_optional(args: argparse.Namespace, paths: Paths) -> str | None:
    if getattr(args, "repo", None):
        return args.repo
    here = locate.slot_for_cwd(paths)
    return here.repo_name if here is not None else None


def _resolve_branch(args: argparse.Namespace, paths: Paths) -> str:
    if getattr(args, "branch", None):
        return args.branch
    here = locate.slot_for_cwd(paths)
    if here is None:
        raise git.GitError(
            "pass <branch> (or run from inside a pm worktree slot)."
        )
    current = git.current_branch(here.path)
    if not current:
        raise git.GitError(
            f"worktree {here.path} is detached; pass <branch> explicitly."
        )
    return current


def _target(args: argparse.Namespace, paths: Paths) -> SelectorTarget:
    return SelectorTarget(
        repo_name=_resolve_repo(args, paths),
        branch=_resolve_branch(args, paths),
    )


def _resolve_on_spec(
    paths: Paths,
    repo_name: str,
    on_spec: str,
    *,
    fallback_branch: str | None = None,
) -> ParentLocator:
    """Resolve an `--on` value into a `ParentLocator`.

    Keywords `current` / `parent` resolve against the cwd's pm slot;
    `fallback_branch` is substituted when `--on current` is used in a
    context where the target branch overrides the slot's current (e.g.
    `create --replace <b>` adopts `b` and `--on current` should point
    to the slot's branch). Any other value is a literal branch name.
    """
    if on_spec == "current":
        here = locate.slot_for_cwd(paths)
        if here is None:
            raise git.GitError(
                "--on current requires running from a pm worktree slot."
            )
        current = fallback_branch or git.current_branch(here.path)
        if not current:
            raise git.GitError(
                "--on current: current worktree has no branch checked out."
            )
        return ParentLocator(repo_name=repo_name, branch=current)
    if on_spec == "parent":
        here = locate.slot_for_cwd(paths)
        if here is None:
            raise git.GitError(
                "--on parent requires running from a pm worktree slot."
            )
        current = git.current_branch(here.path)
        if not current:
            raise git.GitError(
                "--on parent: current worktree is detached; pass a branch instead."
            )
        svc = _service(paths)
        tracked = svc.db.get_branch(repo_name, current)
        if tracked is None:
            raise git.GitError(
                f"{current} is not tracked; cannot resolve --on parent."
            )
        return ParentLocator(
            repo_name=tracked.parent_repo_name, branch=tracked.parent_branch,
        )
    # Literal branch reference — may include cross-repo `repo:branch` form.
    return selectors.resolve_parent_for_base(paths, repo_name, on_spec)


def _scope(args: argparse.Namespace) -> Scope:
    if getattr(args, "all", False):
        return "all"
    return "current"


def _scope_spec(args: argparse.Namespace, *, include_only: bool = False) -> ScopeSpec:
    return ScopeSpec(
        scope=_scope(args),
        skip_ancestors=args.skip_ancestors,
        skip_descendants=args.skip_descendants,
        only=args.only if include_only else False,
        from_branch=args.from_branch,
    )


def _on_spec_for_create(args: argparse.Namespace) -> str:
    """Return the resolved --on keyword, erroring when replace needs an explicit value."""
    if args.on:
        return args.on
    if args.replace:
        raise git.GitError("--replace requires --on <parent-branch>.")
    return "current"


def _run(op: Callable[[StackerService], str]) -> int:
    paths = config.load()
    try:
        result = op(_service(paths))
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    print(result)
    return 0


# --- commands --------------------------------------------------------------


def _cmd_create(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _service(paths)
    pooldb = PoolDB(paths.pool_db())
    try:
        repo_name = _resolve_repo(args, paths)
        on_spec = _on_spec_for_create(args)
        parent = _resolve_on_spec(
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
        paths, pooldb, repo_name, wait=ops_slot.WaitOptions(progress=_stderr_progress),
    )
    return claimed.path, lambda: pooldb.release(claimed.repo, claimed.uuid)


def _cmd_sync(args: argparse.Namespace) -> int:
    paths = config.load()
    if getattr(args, "continue_", False):
        return _run(lambda svc: svc.continue_operation(_resolve_repo(args, svc.paths)))
    if getattr(args, "abort", False):
        return _run(lambda svc: svc.abort_operation(_resolve_repo(args, svc.paths)))
    try:
        target = _target(args, paths)
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return _run(lambda svc: svc.sync(target, _scope_spec(args)))


def _cmd_push(args: argparse.Namespace) -> int:
    options = PushOptions(
        scope=_scope_spec(args, include_only=True),
        draft=args.draft,
        publish=args.publish,
        create_pr=args.create_pr,
    )
    return _run(lambda svc: svc.push(_target(args, svc.paths), options))


def _cmd_ls(args: argparse.Namespace) -> int:
    paths = config.load()
    scope: Scope = _scope(args)
    if scope == "current":
        try:
            target = _target(args, paths)
        except git.GitError as e:
            print(f"pm: {e}", file=sys.stderr)
            return 2
        return _run(
            lambda svc: svc.ls_text(
                target.repo_name,
                target_branch=target.branch,
                scope="current",
                details=args.details,
                json_output=args.json,
            )
        )
    return _run(
        lambda svc: svc.ls_text(
            _resolve_repo_optional(args, svc.paths),
            scope="all",
            details=args.details,
            json_output=args.json,
        )
    )


def _cmd_remove(args: argparse.Namespace) -> int:
    return _run(
        lambda svc: svc.remove(
            _target(args, svc.paths),
            keep_branch=args.keep_branch,
            parent_cascade=args.parent,
            force=args.force,
        )
    )


def _cmd_reparent(args: argparse.Namespace) -> int:
    paths = config.load()
    if getattr(args, "continue_", False):
        return _run(lambda svc: svc.continue_operation(_resolve_repo(args, svc.paths)))
    if getattr(args, "abort", False):
        return _run(lambda svc: svc.abort_operation(_resolve_repo(args, svc.paths)))
    if not args.new_parent:
        print("pm: reparent requires <new-parent> (or --continue / --abort).",
              file=sys.stderr)
        return 2
    try:
        target = _target(args, paths)
        new_parent = _resolve_on_spec(paths, target.repo_name, args.new_parent)
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return _run(lambda svc: svc.reparent(target, new_parent))


def _cmd_split(args: argparse.Namespace) -> int:
    return _run(
        lambda svc: svc.split(
            _target(args, svc.paths),
            args.new_name,
            args.commit,
            stay=args.stay,
        )
    )


def _cmd_rename(args: argparse.Namespace) -> int:
    return _run(
        lambda svc: svc.rename(_target(args, svc.paths), args.new_name)
    )


def _cmd_log(args: argparse.Namespace) -> int:
    return _run(lambda svc: svc.log_text(_target(args, svc.paths)))


def _cmd_continue(args: argparse.Namespace) -> int:
    return _run(lambda svc: svc.continue_operation(_resolve_repo(args, svc.paths)))


def _cmd_abort(args: argparse.Namespace) -> int:
    return _run(lambda svc: svc.abort_operation(_resolve_repo(args, svc.paths)))


def _cmd_guard_no_rebase(args: argparse.Namespace) -> int:
    del args
    paths = config.load()
    service = _service(paths)
    try:
        service.guard_no_rebase()
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    paths = config.load()
    service = _service(paths)
    try:
        repo_name = _resolve_repo(args, paths)
        return _dispatch_config(service, args, repo_name)
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2


def _dispatch_config(
    service: StackerService, args: argparse.Namespace, repo_name: str
) -> int:
    if args.list:
        return _config_list(service, args, repo_name)
    if args.unset:
        return _config_unset(service, args, repo_name)
    if args.key is None:
        print("pm: config requires a key (or --list / --unset).", file=sys.stderr)
        return 2
    if args.value is None:
        return _config_get(service, args, repo_name)
    return _config_set(service, args, repo_name)


def _config_list(
    service: StackerService, args: argparse.Namespace, repo_name: str
) -> int:
    if args.key is not None or args.value is not None:
        print("pm: --list takes no key/value arguments.", file=sys.stderr)
        return 2
    for key, value in service.list_config(repo_name):
        print(f"{key}={value}")
    return 0


def _config_unset(
    service: StackerService, args: argparse.Namespace, repo_name: str
) -> int:
    if args.key is None or args.value is not None:
        print("pm: --unset requires exactly one key.", file=sys.stderr)
        return 2
    return 0 if service.unset_config(repo_name, args.key) else 1


def _config_get(
    service: StackerService, args: argparse.Namespace, repo_name: str
) -> int:
    current = service.get_config(repo_name, args.key)
    if current is None:
        return 1
    print(current)
    return 0


def _config_set(
    service: StackerService, args: argparse.Namespace, repo_name: str
) -> int:
    for note in service.set_config(repo_name, args.key, args.value):
        print(note, file=sys.stderr)
    return 0


# --- parser wiring ---------------------------------------------------------


_Handler = Callable[[argparse.Namespace], int]

_DETAILS_CHOICES: tuple[Details, ...] = ("none", "status", "status-counts", "all")


def _add_repo_branch(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", default=None, help="defaults to the cwd's pm slot")
    p.add_argument(
        "branch", nargs="?", default=None,
        help="defaults to the cwd's current branch",
    )


def _add_scope_flags(p: argparse.ArgumentParser) -> None:
    group = p.add_mutually_exclusive_group()
    group.add_argument(
        "-c", "--current", action="store_false", dest="all",
        help="current lineage: ancestors + target + descendants (default)",
    )
    group.add_argument(
        "-a", "--all", action="store_true",
        help="every tracked branch in the repo",
    )
    p.set_defaults(all=False)
    p.add_argument(
        "--skip-ancestors", action="store_true",
        help="exclude ancestors from the walk (current + descendants)",
    )
    p.add_argument(
        "--skip-descendants", action="store_true",
        help="exclude descendants from the walk (ancestors + current)",
    )
    p.add_argument(
        "--from", dest="from_branch", default=None,
        help="start the walk from this branch (skip earlier entries)",
    )


def _add_create(sub: argparse._SubParsersAction) -> None:
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
    p.set_defaults(func=_cmd_create)


def _add_sync(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("sync", help="cherry-pick branches onto their parents")
    _add_repo_branch(p)
    _add_scope_flags(p)
    p.add_argument(
        "--continue", action="store_true", dest="continue_",
        help="resume a paused cherry-pick (same as `pm stacker continue`)",
    )
    p.add_argument(
        "--abort", action="store_true",
        help="abort the paused op (same as `pm stacker abort`)",
    )
    p.set_defaults(func=_cmd_sync)


def _add_push(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("push", help="force-push + create/update PRs for a scope")
    _add_repo_branch(p)
    _add_scope_flags(p)
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
        default=True, type=_parse_bool,
        help="create/update PRs (default: true); pass false to only force-push",
    )
    p.set_defaults(func=_cmd_push)


def _add_ls(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("ls", help="render the stack tree")
    _add_repo_branch(p)
    _add_scope_flags(p)
    p.add_argument(
        "--details", default="status-counts", choices=_DETAILS_CHOICES,
        help="per-branch detail level (default: status-counts)",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=_cmd_ls)


def _add_remove(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "remove",
        help="untrack a branch (optionally delete it); reparent children",
    )
    _add_repo_branch(p)
    p.add_argument("--force", action="store_true", help="skip confirmation")
    p.add_argument(
        "--parent", action="store_true",
        help="also remove all tracked ancestors up the chain",
    )
    p.add_argument(
        "--keep-branch", action="store_true",
        help="untrack without deleting the git branch (old `untrack` behavior)",
    )
    p.set_defaults(func=_cmd_remove)


def _add_reparent(sub: argparse._SubParsersAction) -> None:
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
    p.set_defaults(func=_cmd_reparent)


def _add_split(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "split",
        help="move commits [<commit>..HEAD] onto a new child branch",
    )
    p.add_argument("--repo", default=None, help="defaults to the cwd's pm slot")
    p.add_argument("--branch", dest="branch", default=None,
                   help="source branch (defaults to cwd's current branch)")
    p.add_argument("new_name", help="name for the new child branch")
    p.add_argument("commit", help="split point (included in the moved range)")
    p.add_argument(
        "--stay", action="store_true",
        help="skip claiming a pool slot for the new branch (ref + DB row still land)",
    )
    p.set_defaults(func=_cmd_split)


def _add_rename(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("rename", help="rename the current branch")
    p.add_argument("--repo", default=None, help="defaults to the cwd's pm slot")
    p.add_argument("--branch", dest="branch", default=None,
                   help="branch to rename (defaults to cwd's current branch)")
    p.add_argument("new_name", help="new branch name")
    p.set_defaults(func=_cmd_rename)


def _add_log(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("log", help="show commits since the branch's managed base")
    _add_repo_branch(p)
    p.set_defaults(func=_cmd_log)


def _add_op_control(sub: argparse._SubParsersAction) -> None:
    for name, handler, help_text in (
        ("continue", _cmd_continue, "resume a paused stacker operation"),
        ("abort", _cmd_abort, "abort a paused stacker operation and reset state"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--repo", default=None, help="defaults to the cwd's pm slot")
        p.set_defaults(func=handler)


def _add_guard(sub: argparse._SubParsersAction) -> None:
    guard = sub.add_parser("guard", help="internal guardrails for Git hooks")
    guard_sub = guard.add_subparsers(dest="guard_cmd", required=True)
    no_rebase = guard_sub.add_parser(
        "no-rebase",
        help="exit nonzero when cwd is a stacker-tracked branch",
    )
    no_rebase.set_defaults(func=_cmd_guard_no_rebase)


def _add_config(sub: argparse._SubParsersAction) -> None:
    cfg = sub.add_parser("config", help="get/set per-repo stacker config (git-config style)")
    cfg.add_argument("--repo", default=None, help="defaults to the cwd's pm slot")
    group = cfg.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="list all set keys")
    group.add_argument("--unset", action="store_true", help="remove a key")
    cfg.add_argument("key", nargs="?", help="config key (e.g. pr.mode)")
    cfg.add_argument("value", nargs="?", help="value to set; omit to read")
    cfg.set_defaults(func=_cmd_config)


def _parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("true", "1", "yes", "y"):
        return True
    if lowered in ("false", "0", "no", "n"):
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {value!r}")


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    stacker = subparsers.add_parser(
        "stacker", help="branch-stack tracking against pm's pool"
    )
    sub = stacker.add_subparsers(dest="cmd", required=True)
    _add_create(sub)
    _add_sync(sub)
    _add_push(sub)
    _add_ls(sub)
    _add_remove(sub)
    _add_reparent(sub)
    _add_split(sub)
    _add_rename(sub)
    _add_log(sub)
    _add_op_control(sub)
    _add_config(sub)
    _add_guard(sub)


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
