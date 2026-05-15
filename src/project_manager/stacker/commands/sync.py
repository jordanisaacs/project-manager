"""`pm stacker sync`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import StackerScope
from project_manager.stacker import git
from project_manager.stacker.models import SyncOptions

from . import _common, stacker_app


@stacker_app.command
def sync(
    branch: str | None = None,
    scope: StackerScope = StackerScope(),
    *,
    continue_: Annotated[
        bool,
        Parameter(name="--continue", negative="", help="resume a paused cherry-pick"),
    ] = False,
    abort: Annotated[
        bool,
        Parameter(negative="", help="abort the paused op"),
    ] = False,
    allow_drop_parent_modifications: Annotated[
        bool,
        Parameter(
            negative="",
            help="drop out-of-band history changes below the stack range and "
            "replay only the branch's working commits onto the parent.",
        ),
    ] = False,
    allow_drop_merge: Annotated[
        bool,
        Parameter(
            negative="",
            help="when the branch's PR is merged but the squash isn't on the "
            "parent, drop the branch's commits and reset it to the parent.",
        ),
    ] = False,
    offline: Annotated[
        bool,
        Parameter(
            negative="",
            help="skip the PR-state refresh; use cached pr_state for the "
            "merged-PR collapse decision.",
        ),
    ] = False,
) -> int:
    """Cherry-pick branches onto their parents.

    Sync always replays exactly the commits the child added on top of its
    recorded base (`managed_base..HEAD`).

    Two preflight gates protect the replay:

    - `--allow-drop-parent-modifications`: required when the branch's
      history below the stack range was rewritten outside stacker (e.g.
      `git rebase --onto`, amends touching pre-stack commits). Without
      the flag, sync errors; with it, those changes are dropped.

    - Merged-PR collapse: when a branch's PR is `MERGED`, sync skips the
      cherry-pick path and resets the branch to its parent — it becomes
      a no-commit branch. If the squash isn't actually on the parent
      yet, `--allow-drop-merge` is required to drop the branch's commits
      anyway.

    `--offline` skips the upfront PR-state refresh (one batched GraphQL
    call per repo) and uses whatever's already cached.

    `--continue` / `--abort` are shortcuts for `pm stacker continue` /
    `abort`.
    """
    paths = config.load()
    svc = _common.service(paths)
    if continue_ or abort:
        if allow_drop_parent_modifications or allow_drop_merge or offline:
            raise git.GitError(
                "--allow-drop-* and --offline cannot be combined with --continue or --abort.",
            )
        if continue_:
            return _common.emit(svc.continue_operation(_common.resolve_repo(scope.repo, paths)))
        return _common.emit(svc.abort_operation(_common.resolve_repo(scope.repo, paths)))
    target = _common.target(scope.repo, branch, paths)
    return _common.emit(
        svc.sync(
            target,
            _common.scope_spec(scope),
            options=SyncOptions(
                allow_drop_parent_modifications=allow_drop_parent_modifications,
                allow_drop_merge=allow_drop_merge,
                offline=offline,
            ),
        )
    )
