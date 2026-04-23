from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from project_manager.paths import Paths

from .db import StackerDB
from .git import GitClient, SubprocessGitClient
from .pr_backend import GhCliBackend, PRBackend


@dataclass(frozen=True)
class StackerCtx:
    """Bundle of deps threaded through every ops/pr/render/cherry_pick function.

    Lifetime: one CLI invocation. Mirrors gitstack's CommandCtx. Construct via
    StackerCtx.default(paths) or explicitly in tests to inject a fake PR backend
    / git client / progress sink.
    """

    db: StackerDB
    paths: Paths
    progress: Callable[[str], None] | None
    pr_backend: PRBackend
    git: GitClient

    @classmethod
    def default(
        cls,
        paths: Paths,
        *,
        db: StackerDB | None = None,
        progress: Callable[[str], None] | None = None,
        pr_backend: PRBackend | None = None,
        git: GitClient | None = None,
    ) -> StackerCtx:
        return cls(
            db=db or StackerDB(paths.stacker_db()),
            paths=paths,
            progress=progress,
            pr_backend=pr_backend or GhCliBackend(),
            git=git or SubprocessGitClient(),
        )
