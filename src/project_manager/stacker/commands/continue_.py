"""`pm stacker continue`."""
from project_manager import config
from project_manager.cli._shared import RepoFlag

from . import _common, stacker_app


@stacker_app.command
def continue_(flag: RepoFlag = RepoFlag()) -> int:
    """Resume a paused stacker operation."""
    paths = config.load()
    svc = _common.service(paths)
    return _common.emit(
        svc.continue_operation(_common.resolve_repo(flag.repo, paths))
    )
