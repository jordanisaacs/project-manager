"""`pm stacker log`."""

from project_manager import config
from project_manager.cli._shared import RepoFlag

from . import _common, stacker_app


@stacker_app.command
def log(branch: str | None = None, flag: RepoFlag = RepoFlag()) -> int:
    """Show commits since the branch's managed base."""
    paths = config.load()
    svc = _common.service(paths)
    return _common.emit(svc.log_text(_common.target(flag.repo, branch, paths)))
