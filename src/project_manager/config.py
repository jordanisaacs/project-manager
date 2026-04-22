import os
import tomllib
from pathlib import Path

from project_manager.paths import Paths

DEFAULT_REPOS = "~/.repos"
DEFAULT_WORKTREES = "~/.worktrees"
DEFAULT_PROJECTS = "~/.projects"
DEFAULT_STACKER_ROOT = "~/.stacker"


def _expand(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser()


def _resolve_config_path() -> Path | None:
    if explicit := os.environ.get("PM_CONFIG"):
        return Path(explicit)
    if xdg := os.environ.get("XDG_CONFIG_HOME"):
        candidate = Path(xdg) / "pm" / "config.toml"
        if candidate.exists():
            return candidate
    candidate = Path.home() / ".config" / "pm" / "config.toml"
    if candidate.exists():
        return candidate
    return None


def load() -> Paths:
    config_path = _resolve_config_path()
    repos = DEFAULT_REPOS
    worktrees = DEFAULT_WORKTREES
    projects = DEFAULT_PROJECTS
    stacker_root = DEFAULT_STACKER_ROOT

    if config_path is not None:
        with config_path.open("rb") as f:
            data = tomllib.load(f)
        paths_section = data.get("paths", {})
        repos = paths_section.get("repos", repos)
        worktrees = paths_section.get("worktrees", worktrees)
        projects = paths_section.get("projects", projects)
        stacker_root = paths_section.get("stacker_root", stacker_root)

    return Paths(
        repos=_expand(repos),
        worktrees=_expand(worktrees),
        projects=_expand(projects),
        stacker_root=_expand(stacker_root),
    )
