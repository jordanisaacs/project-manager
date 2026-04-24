"""Map a pm project to the set of cwds an agent session could have recorded.

Agents (Claude, Codex, Cursor) all persist the working directory where a
session was started. `pm agent ls` groups sessions by pm project, so for
each project we need the set of paths that a session's cwd could equal:
the project directory itself, its resolved real path, each worktree
symlink inside the project, and each symlink's resolved target. Sessions
started from any of those will match the project.

This is the forward-direction counterpart to `project.current`'s reverse
helpers (cwd → project).
"""

from pathlib import Path

from project_manager.paths import Paths
from project_manager.project import discovery


def owned_paths(paths: Paths, project: str) -> set[Path]:
    """Return every path an agent might record as cwd for this project.

    Silently skips any symlink whose target doesn't resolve — a broken
    worktree link shouldn't abort listing for the rest of the project.
    """
    project_dir = paths.project(project)
    out: set[Path] = {project_dir}
    out.add(_safe_resolve(project_dir))
    for wt, repo, slot_uuid in discovery.read_wts(paths, project):
        forward = paths.forward(project, wt)
        out.add(forward)
        out.add(_safe_resolve(forward))
        out.add(_safe_resolve(paths.slot(repo, slot_uuid)))
    return out


def _safe_resolve(p: Path) -> Path:
    """`Path.resolve(strict=False)` that never raises on missing components."""
    try:
        return p.resolve()
    except OSError:
        return p
