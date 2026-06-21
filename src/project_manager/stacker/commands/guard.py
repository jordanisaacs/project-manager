"""`pm stacker guard` — internal guardrails for Git hooks."""

from typing import Annotated

from cyclopts import App, Parameter

from project_manager import config, render

from . import _common, stacker_app

_guard_app = stacker_app.command(
    App(name="guard", help="internal guardrails for Git hooks"),
)


@_guard_app.command
def no_rebase(*, json: Annotated[bool, Parameter(negative="")] = False) -> int:
    """Exit nonzero when cwd is a stacker-tracked branch.

    `guard_no_rebase` raises (→ nonzero exit) when blocked; reaching the
    return means the cwd is clear. `--json` emits `{ok, command, blocked}`
    on that clear path (the blocked case still surfaces as a nonzero error).
    """
    paths = config.load()
    svc = _common.service(paths)
    svc.guard_no_rebase()
    if json:
        render.emit_json({"ok": True, "command": "guard", "blocked": False})
    return 0
