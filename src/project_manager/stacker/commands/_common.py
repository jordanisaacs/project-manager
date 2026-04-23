from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from project_manager import config
from project_manager.paths import Paths
from project_manager.stacker import git, locate, selectors
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import ParentLocator, Scope, ScopeSpec, SelectorTarget
from project_manager.stacker.service import StackerService


def service(paths: Paths) -> StackerService:
    db = StackerDB(paths.stacker_db())
    return StackerService(db, paths, progress=stderr_progress)


def stderr_progress(msg: str) -> None:
    print(f"pm: {msg}", file=sys.stderr)


def resolve_repo(args: argparse.Namespace, paths: Paths) -> str:
    if getattr(args, "repo", None):
        return args.repo
    here = locate.slot_for_cwd(paths)
    if here is None:
        raise git.GitError(
            "pass --repo <name> (or run from inside a pm worktree slot)."
        )
    return here.repo_name


def resolve_repo_optional(args: argparse.Namespace, paths: Paths) -> str | None:
    if getattr(args, "repo", None):
        return args.repo
    here = locate.slot_for_cwd(paths)
    return here.repo_name if here is not None else None


def resolve_branch(args: argparse.Namespace, paths: Paths) -> str:
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


def target(args: argparse.Namespace, paths: Paths) -> SelectorTarget:
    return SelectorTarget(
        repo_name=resolve_repo(args, paths),
        branch=resolve_branch(args, paths),
    )


def resolve_on_spec(
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
        svc = service(paths)
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


def scope(args: argparse.Namespace) -> Scope:
    if getattr(args, "all", False):
        return "all"
    return "current"


def scope_spec(
    args: argparse.Namespace, *, include_only: bool = False
) -> ScopeSpec:
    return ScopeSpec(
        scope=scope(args),
        skip_ancestors=args.skip_ancestors,
        skip_descendants=args.skip_descendants,
        only=args.only if include_only else False,
        from_branch=args.from_branch,
    )


def run(op: Callable[[StackerService], str]) -> int:
    paths = config.load()
    try:
        result = op(service(paths))
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    print(result)
    return 0


def parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in ("true", "1", "yes", "y"):
        return True
    if lowered in ("false", "0", "no", "n"):
        return False
    raise argparse.ArgumentTypeError(f"expected true/false, got {value!r}")


def add_repo_branch(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", default=None, help="defaults to the cwd's pm slot")
    p.add_argument(
        "branch", nargs="?", default=None,
        help="defaults to the cwd's current branch",
    )


def add_scope_flags(p: argparse.ArgumentParser) -> None:
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
