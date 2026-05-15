"""`pm stacker repair`."""

from project_manager import config
from project_manager.cli._shared import RepoFlag

from . import _common, stacker_app


@stacker_app.command
def repair(
    base_ref: str,
    *,
    branch: str | None = None,
    flag: RepoFlag = RepoFlag(),
) -> int:
    """Reset stored managed-base / last-synced / last-clean-head to match git.

    <base_ref>'s tip becomes the new managed_base (e.g. master, HEAD~3).
    """
    paths = config.load()
    svc = _common.service(paths)
    return _common.emit(svc.repair(_common.target(flag.repo, branch, paths), base_ref))
