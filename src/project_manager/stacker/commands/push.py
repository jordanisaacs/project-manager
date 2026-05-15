"""`pm stacker push`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import StackerScope
from project_manager.stacker.models import PushOptions

from . import _common, stacker_app


@stacker_app.command
def push(
    branch: str | None = None,
    scope: StackerScope = StackerScope(),
    *,
    only: Annotated[bool, Parameter(negative="")] = False,
    draft: Annotated[bool, Parameter(negative="")] = False,
    publish: Annotated[bool, Parameter(negative="")] = False,
    create_pr: bool = True,
) -> int:
    """Force-push + create/update PRs for a scope.

    `--draft` / `--publish` are mutually exclusive. `--no-create-pr` force-pushes
    without touching PRs.
    """
    if draft and publish:
        raise ValueError("--draft and --publish are mutually exclusive.")
    paths = config.load()
    svc = _common.service(paths)
    options = PushOptions(
        scope=_common.scope_spec(scope, only=only),
        draft=draft,
        publish=publish,
        create_pr=create_pr,
    )
    return _common.emit(svc.push(_common.target(scope.repo, branch, paths), options))
