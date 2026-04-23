from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import get_args

from . import git
from .models import PRMode

PR_MODE = "pr.mode"
PR_TRUNK = "pr.trunk"
PR_TARGET_REPO = "pr.target-repo"

PR_MODES: tuple[str, ...] = get_args(PRMode)
DEFAULT_MODE: PRMode = "pr-pr"

_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def _validate_mode(value: str) -> None:
    if value not in PR_MODES:
        raise git.GitError(
            f"pr.mode must be one of {sorted(PR_MODES)}; got {value!r}."
        )


def _validate_trunk(value: str) -> None:
    if not value.strip():
        raise git.GitError("pr.trunk must be a non-empty branch name.")


def _validate_target_repo(value: str) -> None:
    if not _REPO_SLUG_RE.match(value):
        raise git.GitError(
            f"pr.target-repo must be in 'owner/repo' form; got {value!r}."
        )


@dataclass(frozen=True)
class _KeySpec:
    key: str
    validator: Callable[[str], None]
    description: str


CONFIG_KEYS: dict[str, _KeySpec] = {
    PR_MODE: _KeySpec(
        key=PR_MODE,
        validator=_validate_mode,
        description=f"PR topology: one of {sorted(PR_MODES)}",
    ),
    PR_TRUNK: _KeySpec(
        key=PR_TRUNK,
        validator=_validate_trunk,
        description="Trunk branch name (e.g. master, main).",
    ),
    PR_TARGET_REPO: _KeySpec(
        key=PR_TARGET_REPO,
        validator=_validate_target_repo,
        description="Repo where PRs are opened, as owner/repo. Defaults to the push remote.",
    ),
}


def require_known_key(key: str) -> _KeySpec:
    spec = CONFIG_KEYS.get(key)
    if spec is None:
        valid = ", ".join(sorted(CONFIG_KEYS))
        raise git.GitError(
            f"Unknown config key {key!r}. Valid keys: {valid}."
        )
    return spec


def validate_value(key: str, value: str) -> None:
    require_known_key(key).validator(value)


def parse_mode(value: str | None) -> PRMode:
    """Resolve a stored pr.mode string into a typed PRMode.

    Unset → default. Any other value must be a known mode; invalid rows
    surface as errors rather than silently defaulting, so a manually edited
    DB doesn't pretend to be OK.
    """
    match value:
        case None:
            return DEFAULT_MODE
        case "pr-pr":
            return "pr-pr"
        case "repo-pr":
            return "repo-pr"
        case _:
            raise git.GitError(
                f"Stored {PR_MODE}={value!r} is not one of {sorted(PR_MODES)}. "
                "Fix with `pm stacker config pr.mode <value>`."
            )
