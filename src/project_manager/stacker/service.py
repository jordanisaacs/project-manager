"""StackerService — thin public facade over ops/pr/render/cherry_pick modules.

The heavy lifting lives in:
  - stacker.ops.*     — high-level orchestration (init, sync, push, …)
  - stacker.pr.*      — PR lifecycle (config, resolve, find, stack block)
  - stacker.cherry_pick.* — local cherry-pick state machine
  - stacker.render.*  — text/JSON/graph output

This facade just bundles deps into a StackerCtx and forwards 20 public
methods. No private passthroughs — tests that need internals import
them directly from the new modules.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from project_manager.paths import Paths

from .ctx import StackerCtx
from .db import StackerDB
from .git import GitClient, SubprocessGitClient
from .models import (
    DEFAULT_PUSH_OPTIONS,
    DEFAULT_SCOPE,
    ParentLocator,
    PushOptions,
    ScopeSpec,
    SelectorTarget,
    TrackedBranch,
    WorktreeInit,
)
from .ops import (
    continue_abort,
    guard,
    init,
)
from .ops import (
    push as push_ops,
)
from .ops import (
    remove as remove_ops,
)
from .ops import (
    rename as rename_ops,
)
from .ops import (
    reparent as reparent_ops,
)
from .ops import (
    split as split_ops,
)
from .ops import (
    sync as sync_ops,
)
from .ops import (
    track as track_ops,
)
from .pr.config import get_config as pr_get_config
from .pr.config import list_config as pr_list_config
from .pr.config import set_config as pr_set_config
from .pr.config import unset_config as pr_unset_config
from .pr_backend import GhCliBackend, PRBackend
from .render import log as render_log
from .render import ls as render_ls
from .render import status as render_status
from .render.ls import LsOptions

if TYPE_CHECKING:
    pass


class StackerService:
    def __init__(
        self,
        db: StackerDB,
        paths: Paths,
        *,
        progress: Callable[[str], None] | None = None,
        pr_backend: PRBackend | None = None,
        git: GitClient | None = None,
    ) -> None:
        self._ctx = StackerCtx(
            db=db,
            paths=paths,
            progress=progress,
            pr_backend=pr_backend or GhCliBackend(),
            git=git or SubprocessGitClient(),
        )

    # --- properties preserved for tests that read service.<attr> ---

    @property
    def db(self) -> StackerDB:
        return self._ctx.db

    @property
    def paths(self) -> Paths:
        return self._ctx.paths

    @property
    def pr_backend(self) -> PRBackend:
        return self._ctx.pr_backend

    @property
    def progress(self) -> Callable[[str], None] | None:
        return self._ctx.progress

    @property
    def ctx(self) -> StackerCtx:
        """Expose the StackerCtx for tests that want to call module-level functions."""
        return self._ctx

    # --- init / track ---

    def init_new_branch(self, spec: WorktreeInit) -> TrackedBranch:
        return init.init_new_branch(self._ctx, spec)

    def init_adopt_branch(self, spec: WorktreeInit) -> TrackedBranch | None:
        return init.init_adopt_branch(self._ctx, spec)

    def create_tracked_branch(
        self,
        repo_name: str,
        branch: str,
        parent: ParentLocator,
        *,
        copy_from: str | None = None,
    ) -> TrackedBranch:
        return track_ops.create_tracked_branch(
            self._ctx, repo_name, branch, parent, copy_from=copy_from
        )

    def track(
        self, target: SelectorTarget, parent: ParentLocator
    ) -> TrackedBranch:
        return track_ops.track(self._ctx, target, parent)

    # --- mutate ---

    def remove(
        self,
        target: SelectorTarget,
        *,
        keep_branch: bool = False,
        parent_cascade: bool = False,
        force: bool = False,
    ) -> str:
        return remove_ops.remove(
            self._ctx,
            target,
            keep_branch=keep_branch,
            parent_cascade=parent_cascade,
            force=force,
        )

    def reparent(self, target: SelectorTarget, new_parent: ParentLocator) -> str:
        return reparent_ops.reparent(self._ctx, target, new_parent)

    def split(
        self,
        target: SelectorTarget,
        new_name: str,
        split_commit: str,
        *,
        stay: bool = False,
    ) -> str:
        return split_ops.split(
            self._ctx, target, new_name, split_commit, stay=stay
        )

    def rename(self, target: SelectorTarget, new_name: str) -> str:
        return rename_ops.rename(self._ctx, target, new_name)

    # --- sync / push / repair ---

    def sync(
        self, target: SelectorTarget, spec: ScopeSpec = DEFAULT_SCOPE
    ) -> str:
        return sync_ops.sync(self._ctx, target, spec)

    def repair(self, target: SelectorTarget, base_ref: str) -> str:
        return sync_ops.repair(self._ctx, target, base_ref)

    def push(
        self,
        target: SelectorTarget,
        options: PushOptions = DEFAULT_PUSH_OPTIONS,
    ) -> str:
        return push_ops.push(self._ctx, target, options)

    # --- op control / guard ---

    def continue_operation(self, repo_name: str) -> str:
        return continue_abort.continue_operation(self._ctx, repo_name)

    def abort_operation(self, repo_name: str) -> str:
        return continue_abort.abort_operation(self._ctx, repo_name)

    def guard_no_rebase(self) -> None:
        guard.guard_no_rebase(self._ctx)

    # --- rendering ---

    def status_text(self, target: SelectorTarget) -> str:
        return render_status.status_text(self._ctx, target)

    def parent_text(self, target: SelectorTarget) -> str:
        return render_status.parent_text(self._ctx, target)

    def log_text(self, target: SelectorTarget) -> str:
        return render_log.log_text(self._ctx, target)

    def ls_text(
        self,
        repo_name: str | None = None,
        options: LsOptions | None = None,
    ) -> str:
        return render_ls.ls_text(self._ctx, repo_name, options or LsOptions())

    # --- config ---

    def get_config(self, repo_name: str, key: str) -> str | None:
        return pr_get_config(self._ctx, repo_name, key)

    def set_config(self, repo_name: str, key: str, value: str) -> list[str]:
        return pr_set_config(self._ctx, repo_name, key, value)

    def unset_config(self, repo_name: str, key: str) -> bool:
        return pr_unset_config(self._ctx, repo_name, key)

    def list_config(self, repo_name: str) -> list[tuple[str, str]]:
        return pr_list_config(self._ctx, repo_name)
