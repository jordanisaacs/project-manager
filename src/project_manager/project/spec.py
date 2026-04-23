"""Pure parsers for `pm project` worktree specs (CLI-independent)."""
from project_manager.errors import ProjectError


def parse_wt_spec(value: str) -> list[tuple[str, str]]:
    """Parse `foo,bar:baz,qux` → [("foo","foo"), ("bar","baz"), ("qux","qux")].

    Each comma-separated item is either `<repo>` (wt name = repo) or
    `<wt>:<repo>`. Reject: empty item, empty side of colon, extra colons,
    duplicate wt names.
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            raise ProjectError(f"empty worktree entry in spec: {value!r}")
        match item.split(":"):
            case [only]:
                wt = repo = only.strip()
            case [a, b]:
                wt, repo = a.strip(), b.strip()
            case _:
                raise ProjectError(f"worktree entry has too many colons: {item!r}")
        if not wt or not repo:
            raise ProjectError(f"worktree entry has empty name or repo: {item!r}")
        if wt in seen:
            raise ProjectError(f"duplicate worktree name '{wt}' in spec")
        seen.add(wt)
        out.append((wt, repo))
    return out
