"""`pm stacker repair`."""

from typing import Annotated

from cyclopts import Parameter

from project_manager import config
from project_manager.cli._shared import RepoFlag

from . import _common, stacker_app


@stacker_app.command
def repair(
    base_ref: str,
    *,
    branch: str | None = None,
    flag: RepoFlag = RepoFlag(),
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Reset stored managed-base / last-synced / last-clean-head to match git.

    <base_ref>'s tip becomes the new managed_base (e.g. master, HEAD~3).
    """
    paths = config.load()
    svc = _common.service(paths)
    target = _common.target(flag.repo, branch, paths)
    return _common.emit_result(
        svc.repair(target, base_ref),
        json=json,
        command="repair",
        repo=target.repo_name,
        branch=target.branch,
        paths=paths,
    )
