"""Agent source adapters: one async `fetch` per agent, sharing `SessionEntry`.

Each adapter reads one vendor's on-disk session store (Claude Code's
`~/.claude/projects/`, Codex CLI's `state_5.sqlite`, Cursor CLI's chat
DBs) and returns the top-N sessions whose recorded cwd is in the given
owned-paths set. All adapters are `async` so the orchestrator in
`agent.ls` can fan them out with `asyncio.gather`; internally they wrap
blocking I/O in `asyncio.to_thread` so real concurrency applies.
"""

from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from project_manager.agent.sources import claude, codex, cursor
from project_manager.agent.sources._entry import SessionEntry

__all__ = ["REGISTRY", "SessionEntry", "parse_agents"]


FetchFn = Callable[[set[Path], int], Coroutine[Any, Any, list[SessionEntry]]]

REGISTRY: dict[str, FetchFn] = {
    "claude": claude.fetch,
    "codex": codex.fetch,
    "cursor": cursor.fetch,
}


def parse_agents(raw: str | None) -> frozenset[str]:
    """Resolve `--agent` flag to a set of source names.

    `None` means every registered source. A comma-separated string picks
    a subset. Unknown names raise `ValueError` so `pm agent ls --agent
    claud` surfaces a typo immediately instead of silently returning
    nothing.
    """
    if raw is None:
        return frozenset(REGISTRY)
    selected = {s.strip() for s in raw.split(",") if s.strip()}
    unknown = selected - REGISTRY.keys()
    if unknown:
        raise ValueError(f"unknown agent: {sorted(unknown)[0]}")
    return frozenset(selected)
