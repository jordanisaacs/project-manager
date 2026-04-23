"""`pm project status`."""
from project_manager import check as check_mod
from project_manager import config
from project_manager.project import current
from project_manager.project import status as status_mod

from . import project_app


@project_app.command
def status(project: str | None = None) -> int:
    """Show health + db rows for a single project (defaults to current)."""
    paths = config.load()
    resolved = current.resolve_project(paths, project)
    rows = status_mod.status(paths, resolved)
    for row in rows:
        f = row.finding
        wt = row.wt if row.wt is not None else "-"
        repo = row.repo if row.repo is not None else "-"
        uuid = row.slot_uuid if row.slot_uuid is not None else "-"
        forward = f.forward_path if f.forward_path is not None else "-"
        slot = f.slot_path if f.slot_path is not None else "-"
        print(
            f"{wt}\t{repo}\t{uuid}\t{f.kind.value}\t{forward}\t{slot}\t{f.detail}"
        )
    non_healthy = [r for r in rows if r.finding.kind != check_mod.Kind.ACTIVE]
    return 1 if non_healthy else 0
