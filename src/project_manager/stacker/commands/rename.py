"""`pm stacker rename`."""

from project_manager import config
from project_manager.cli._shared import RepoFlag

from . import _common, stacker_app


@stacker_app.command
def rename(new_name: str, *, branch: str | None = None, flag: RepoFlag = RepoFlag()) -> int:
    """Rename the current branch.

    --branch picks a different source branch (defaults to cwd's current branch).
    """
    paths = config.load()
    svc = _common.service(paths)
    return _common.emit(svc.rename(_common.target(flag.repo, branch, paths), new_name))
