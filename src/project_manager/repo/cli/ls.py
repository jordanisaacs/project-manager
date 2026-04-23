"""`pm repo ls`."""
from project_manager import config
from project_manager.repo import ls as ls_mod

from . import repo_app


def _upstream_str(row: ls_mod.RepoRow) -> str:
    if row.branch is None:
        return "-"
    if not row.has_upstream or row.ahead is None or row.behind is None:
        return "no-upstream"
    if row.ahead == 0 and row.behind == 0:
        return "up-to-date"
    parts: list[str] = []
    if row.ahead:
        parts.append(f"ahead {row.ahead}")
    if row.behind:
        parts.append(f"behind {row.behind}")
    return " ".join(parts)


@repo_app.command
def ls() -> int:
    """List canonical repos with branch + upstream status."""
    paths = config.load()
    for row in ls_mod.ls(paths):
        branch = row.branch if row.branch is not None else "-"
        status = "dirty" if row.dirty else "clean"
        print(f"{row.repo}\t{branch}\t{status}\t{_upstream_str(row)}")
    return 0
