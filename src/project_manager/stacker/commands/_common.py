"""Domain helpers shared across stacker commands.

Pure business-logic utilities — every CLI-specific argparse helper moved
to `project_manager.cli._shared` during the cyclopts migration. The
things remaining here all resolve/construct values the `StackerService`
needs; they know nothing about how the user invoked the command.
"""
import sys

from project_manager import output
from project_manager.cli._shared import StackerScope
from project_manager.paths import Paths
from project_manager.stacker import git, locate, selectors
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import (
    ColorMode,
    ParentLocator,
    Scope,
    ScopeSpec,
    SelectorTarget,
)
from project_manager.stacker.service import StackerService


def service(paths: Paths) -> StackerService:
    db = StackerDB(paths.stacker_db())
    return StackerService(db, paths, progress=stderr_progress)


def stderr_progress(msg: str) -> None:
    print(f"pm: {msg}", file=sys.stderr)


def resolve_repo(repo: str | None, paths: Paths) -> str:
    if repo:
        return repo
    here = locate.slot_for_cwd(paths)
    if here is None:
        raise git.GitError(
            "pass --repo <name> (or run from inside a pm worktree slot)."
        )
    return here.repo_name


def resolve_repo_optional(repo: str | None, paths: Paths) -> str | None:
    if repo:
        return repo
    here = locate.slot_for_cwd(paths)
    return here.repo_name if here is not None else None


def resolve_branch(branch: str | None, paths: Paths) -> str:
    if branch:
        return branch
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


def target(repo: str | None, branch: str | None, paths: Paths) -> SelectorTarget:
    return SelectorTarget(
        repo_name=resolve_repo(repo, paths),
        branch=resolve_branch(branch, paths),
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


def scope_of(s: StackerScope) -> Scope:
    return "all" if s.all else "current"


def scope_spec(s: StackerScope, *, only: bool = False) -> ScopeSpec:
    return ScopeSpec(
        scope=scope_of(s),
        skip_ancestors=s.skip_ancestors,
        skip_descendants=s.skip_descendants,
        only=only,
        from_branch=s.from_branch,
    )


def emit(result: str) -> int:
    """Print a service result string and return exit code 0."""
    print(result)
    return 0


def resolve_color_mode(cli: ColorMode) -> ColorMode:
    """Clamp the CLI color-mode to the effective mode.

    `NO_COLOR` / non-TTY stdout both force `off` regardless of what the
    user passed; otherwise whatever the user asked for wins. `--color-mode
    off` always works, even in a TTY that could show color.
    """
    return cli if output.use_color() else "off"
