"""`pm stacker abort`."""

from project_manager import config
from project_manager.cli._shared import RepoFlag

from . import _common, stacker_app


@stacker_app.command
def abort(flag: RepoFlag = RepoFlag()) -> int:
    """Abort a paused stacker operation and reset state."""
    paths = config.load()
    svc = _common.service(paths)
    return _common.emit(svc.abort_operation(_common.resolve_repo(flag.repo, paths)))
