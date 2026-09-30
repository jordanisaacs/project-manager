"""`pm omnigent` commands."""

from typing import Annotated

from cyclopts import App, Parameter

from project_manager import config, render
from project_manager.cli._shared import root

from . import sync as sync_mod

omnigent_app = root.command(
    App(name="omnigent", help="sync PM projects to Omnigent"),
)

SYNC_COLUMNS = [
    render.Column("Project", "project", style="bold"),
    render.Column("Action", "action"),
    render.Column("Omnigent ID", "remote_id", style="dim"),
    render.Column("Message", "message"),
]


@omnigent_app.command
def sync(
    *,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Backfill and reconcile all PM projects with Omnigent."""
    paths = config.load()
    settings = config.omnigent()
    if settings is None:
        raise ValueError("Omnigent integration is not configured in [omnigent]")
    rows = sync_mod.sync_projects(paths, settings)
    render.emit_rows(rows, SYNC_COLUMNS, as_json=json)
    return 1 if any(row.failed for row in rows) else 0
