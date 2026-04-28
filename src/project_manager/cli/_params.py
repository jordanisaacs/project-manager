"""Unified `--project` / `--repo` parameter definitions for `pm` commands.

Every command that takes a project or repo flag should source its
declaration here so `-p/-r`, help text, and default-resolution stay
consistent across the CLI.

Three forms are exposed:

- `ProjectArg` / `RepoArg` / `RepoListArg`: bare `Annotated` aliases for
  use as keyword-only parameters (required when the function signature
  has `*args` before the flag, as in `pm agent claude`).
- `ProjectFlag` / `RepoFlag`: `@Parameter(name="*")` dataclasses for use
  as flattened scope parameters (the common case across stacker /
  project-wt commands).
- `resolve_project` / `resolve_repo` / `resolve_repo_optional` /
  `parse_repo_list`: defaulting helpers that turn the user-supplied
  flag into a concrete repo/project name (or `None` for the all-repos
  list form).

`resolve_project` is re-exported from `project.current` (its canonical
home, used by non-CLI callers too); the repo resolvers live here because
they're pure CLI-layer helpers — they only translate `flag → name` and
have no other consumers.
"""
from dataclasses import dataclass
from typing import Annotated

from cyclopts import Parameter

from project_manager.paths import Paths
from project_manager.project.current import resolve_project  # re-export
from project_manager.stacker import git, locate

_PROJECT_HELP = "project name (defaults to current project)"
_REPO_HELP = "repo name (defaults to the cwd's pm slot)"
_REPO_OPTIONAL_HELP = "limit to one repo (default: all repos)"
_REPO_LIST_HELP = "comma-separated repo list (defaults to every repo)"

ProjectArg = Annotated[
    str | None,
    Parameter(name=("-p", "--project"), help=_PROJECT_HELP),
]
RepoArg = Annotated[
    str | None,
    Parameter(name=("-r", "--repo"), help=_REPO_HELP),
]
RepoOptionalArg = Annotated[
    str | None,
    Parameter(name=("-r", "--repo"), help=_REPO_OPTIONAL_HELP),
]
RepoListArg = Annotated[
    str | None,
    Parameter(name=("-r", "--repo"), help=_REPO_LIST_HELP),
]


@Parameter(name="*")
@dataclass(frozen=True)
class ProjectFlag:
    """`-p/--project <name>`; defaults to the cwd's current project."""

    project: ProjectArg = None


@Parameter(name="*")
@dataclass(frozen=True)
class RepoFlag:
    """`-r/--repo <name>`; defaults to the cwd's pm slot."""

    repo: RepoArg = None


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


def parse_repo_list(value: str | None) -> list[str] | None:
    """Split a comma-separated `--repo` value; `None` / empty → all repos."""
    if not value:
        return None
    parts = [r.strip() for r in value.split(",") if r.strip()]
    return parts or None


__all__ = [
    "ProjectArg",
    "RepoArg",
    "RepoOptionalArg",
    "RepoListArg",
    "ProjectFlag",
    "RepoFlag",
    "resolve_project",
    "resolve_repo",
    "resolve_repo_optional",
    "parse_repo_list",
]
