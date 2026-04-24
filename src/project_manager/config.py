import os
import tomllib
import zoneinfo
from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path

from project_manager.paths import Paths

DEFAULT_REPOS = "~/.repos"
DEFAULT_WORKTREES = "~/.worktrees"
DEFAULT_PROJECTS = "~/.projects"
DEFAULT_STACKER_ROOT = "~/.stacker"


@dataclass(frozen=True)
class Display:
    """User-facing display preferences loaded from `[display]` in config.

    Kept separate from `Paths` so filesystem layout changes don't
    collide with cosmetic/UI settings. `timezone=None` means "use the
    system local zone" — the default when no `[display].timezone` is
    set.
    """

    timezone: tzinfo | None = None


@dataclass(frozen=True)
class Concurrency:
    """`[concurrency]` knobs shared by every bounded fanout in pm.

    `limit` caps the number of git children run in parallel across every
    (repo, pool-slot) target for `pm repo maintenance`, and bounds the
    per-node / per-repo prefetch fanout in `pm stacker ls`. The default
    of 10 keeps the disk busy on a multi-worktree layout without
    spawning a watchman storm on cold fsmonitor state.
    """

    limit: int = 10


@dataclass(frozen=True)
class Agents:
    """Agent-launch preferences loaded from `[agents]` in config.

    `commands` maps an agent name (matching `AgentName.value` in
    `project_manager.agent.run`) to a shell-like command string —
    parsed by `shlex.split` at exec time, with forwarded user args
    appended. Empty dict means no agents are configured; any
    `pm agent <name>` invocation will raise with a pointer to the
    config file.
    """

    commands: dict[str, str]


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


def display() -> Display:
    """Load the `[display]` section.

    Re-reads the config file rather than attaching display settings to
    `Paths`; keeps existing callers of `config.load()` (every pm
    subcommand) unchanged and lets non-rendering code never touch
    zoneinfo. TOML parsing is cheap and the file is OS-page-cached —
    the second read per invocation is effectively free.
    """
    config_path = _resolve_config_path()
    if config_path is None:
        return Display()
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    section = data.get("display", {})
    tz_name = section.get("timezone")
    if not tz_name:
        return Display()
    try:
        return Display(timezone=zoneinfo.ZoneInfo(tz_name))
    except zoneinfo.ZoneInfoNotFoundError as e:
        raise ValueError(
            f"unknown timezone '{tz_name}' in [display].timezone — "
            f"expected an IANA name like 'America/Los_Angeles'",
        ) from e


def concurrency() -> Concurrency:
    """Load the `[concurrency]` section. Same lazy-read pattern as `display()`."""
    config_path = _resolve_config_path()
    if config_path is None:
        return Concurrency()
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    section = data.get("concurrency", {})
    raw = section.get("limit", Concurrency.limit)
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        raise ValueError(
            f"[concurrency].limit must be a positive integer, got {raw!r}",
        )
    return Concurrency(limit=raw)


def agents() -> Agents:
    """Load the `[agents.commands]` table.

    Same file-reading pattern as `display()` — no caching, so tests
    that set `PM_CONFIG` via monkeypatch observe their own config
    even when a previous test read a different one.

    Schema:
        [agents.commands]
        claude = "isaac"
        codex  = "isaac codex --"
        cursor = "agent"
    """
    config_path = _resolve_config_path()
    if config_path is None:
        return Agents(commands={})
    with config_path.open("rb") as f:
        data = tomllib.load(f)
    commands_raw = data.get("agents", {}).get("commands", {})
    if not isinstance(commands_raw, dict):
        # ValueError (not TypeError) because the source is a user-edited
        # config file, not an internal API call — ValueError is what
        # cli/__init__.py:main catches and renders as `pm: <msg>`.
        raise ValueError(  # noqa: TRY004
            '[agents.commands] must be a table of name = "command"',
        )
    commands: dict[str, str] = {}
    for key, value in commands_raw.items():
        if not isinstance(value, str):
            raise ValueError(  # noqa: TRY004
                f"[agents.commands].{key} must be a string, "
                f"got {type(value).__name__}",
            )
        commands[str(key)] = value
    return Agents(commands=commands)
