"""`pm stacker guard` — internal guardrails for Git hooks."""

from cyclopts import App

from project_manager import config

from . import _common, stacker_app

_guard_app = stacker_app.command(
    App(name="guard", help="internal guardrails for Git hooks"),
)


@_guard_app.command
def no_rebase() -> int:
    """Exit nonzero when cwd is a stacker-tracked branch."""
    paths = config.load()
    svc = _common.service(paths)
    svc.guard_no_rebase()
    return 0
