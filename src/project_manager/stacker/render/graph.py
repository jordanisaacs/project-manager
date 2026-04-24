from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from project_manager.stacker import git, locate, selectors
from project_manager.stacker.models import (
    Details,
    MergedStyle,
    PRState,
    TrackedBranch,
)

from . import format as fmt

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx

    from .ls import RenderOptions


IconKind = Literal[
    "local_only", "no_pr", "pr_open", "merged",
    "pr_draft", "pr_approved",
    "pr_open_comments", "pr_approved_comments",
]

_SYMBOL: dict[IconKind, str] = {
    "local_only": "·",
    "no_pr": "○",
    "pr_open": "●",
    "merged": "■",
    "pr_draft": "▼",
    "pr_open_comments": "⚑",
    "pr_approved": "✓",
    "pr_approved_comments": "◼",
}

# Per-status fg colors for the symbol (and for name/suffix at color_mode
# >= "title"). Mirrors gitstack's apply_color_mode map.
_ICON_COLOR: dict[IconKind, str | None] = {
    "local_only": None,
    "no_pr": None,
    "pr_open": None,
    "merged": None,                 # merged styled via dim/strike, not fg
    "pr_draft": None,
    "pr_open_comments": "red",
    "pr_approved": "green",
    "pr_approved_comments": "red",  # comments win visually over approval
}

_ICON_MEANING: dict[IconKind, str] = {
    "local_only": "local only",
    "no_pr": "pushed, no PR",
    "pr_open": "PR open",
    "merged": "merged",
    "pr_draft": "PR draft",
    "pr_open_comments": "PR open w/ comments",
    "pr_approved": "PR approved",
    "pr_approved_comments": "PR approved w/ comments",
}

_OFFLINE_ICONS: tuple[IconKind, ...] = (
    "local_only", "no_pr", "pr_open", "merged",
)
_ONLINE_ICONS: tuple[IconKind, ...] = (
    "pr_draft", "pr_open_comments", "pr_approved", "pr_approved_comments",
)


@dataclass(frozen=True)
class GraphCtx:
    """Per-repo state for graph rendering; threaded through recursion.

    `details` and `render_opts` are tree-level, not per-node, so they
    live here rather than being passed as parallel args to keep
    `render_graph_node` within the project's arity budget. `pr_states`
    is preloaded once per repo so the renderer never hits the DB
    per-node.
    """

    repo_name: str
    items: list[TrackedBranch]
    live: dict[str, git.WorktreeInfo]
    details: Details
    render_opts: RenderOptions
    pr_states: dict[str, PRState]
    # Branch checked out in the cwd's pm slot for this repo, or None.
    current_branch: str | None = None
    # Per-branch project tag. Populated for any branch checked out in a
    # project-owned pm slot, so the `[<project>]` token can surface on
    # every row (not just current) and help the reader navigate to the
    # worktree that hosts the branch.
    branch_projects: dict[str, str] = field(default_factory=dict)
    # Per-branch worktree label keyed on `(repo_name, branch)`. When
    # set, the node emits `(<label>)` in place of the generic
    # `(current)` marker — lets `pm project status` and `pm stacker
    # ls` (run from a project dir) tag every row with its originating
    # pm worktree name. Keyed on `(repo, branch)` instead of bare
    # branch name so same-named branches across different repos
    # (e.g. `main` in two pool repos) don't collide. An absent entry
    # falls through to the `(current)` fallback for the cwd's branch.
    wt_labels: dict[tuple[str, str], str] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphPos:
    """Branch identity + position of its node in the rendered tree."""

    branch: str
    prefix: str
    is_last: bool
    implicit: bool


@dataclass(frozen=True)
class NodeStatus:
    """Per-branch state consumed by the row renderer.

    Computed up-front by `build_node_status` so text and JSON paths
    share one source. Fields populated only when `--details` warrants
    the underlying git call; the renderer gates on `details` before
    consulting them.
    """

    synced: bool
    commit_count: int
    ahead_of_remote: int
    has_upstream: bool
    dirty: bool
    is_current: bool
    # Stage 2 populates merged from the `pr_state` table; Stage 1 always False.
    merged: bool = False


def render_graph_node(
    ctx: StackerCtx,
    lines: list[str],
    gctx: GraphCtx,
    pos: GraphPos,
) -> None:
    tracked = next((item for item in gctx.items if item.branch == pos.branch), None)
    pr = gctx.pr_states.get(pos.branch) if tracked is not None else None
    status = (
        build_node_status(ctx, gctx, tracked, pr=pr, details=gctx.details)
        if tracked is not None
        else None
    )
    merged = status is not None and status.merged
    needs_sync = status is not None and not status.synced
    connector = _connector(
        is_last=pos.is_last,
        needs_sync=needs_sync and not merged,
        merged=merged,
        details=gctx.details,
        icons=gctx.render_opts.icons,
    )
    gutter = _row_gutter(is_current=status.is_current if status else False)
    icon = _status_symbol(tracked, pr, status, gctx)
    name_text = _name_label(tracked, pr, status, pos, gctx)
    tail = (
        _row_tail(tracked, pr, status, pos, gctx)
        if tracked is not None
        else _implicit_root_tail(pos)
    )
    body = f"{icon}{name_text}{tail}"
    if merged:
        body = _apply_merged_style(body, gctx.render_opts.merged_style)
    line = f"{gutter}{pos.prefix}{connector}{body}"
    lines.append(line)

    children = sorted(
        (item for item in gctx.items if item.parent_branch == pos.branch),
        key=lambda item: item.branch,
    )
    child_prefix = pos.prefix + ("    " if pos.is_last else "│   ")
    for index, child in enumerate(children):
        render_graph_node(
            ctx,
            lines,
            gctx,
            GraphPos(
                branch=child.branch,
                prefix=child_prefix,
                is_last=index == len(children) - 1,
                implicit=False,
            ),
        )


def _row_gutter(*, is_current: bool) -> str:
    """Two-char leading gutter: `> ` for current row, two spaces otherwise."""
    return "> " if is_current else "  "


def _connector(
    *,
    is_last: bool,
    needs_sync: bool,
    merged: bool,
    details: Details,
    icons: bool,
) -> str:
    """Branch connector: standard, `✗`-decorated, or merged `■┄┆ `.

    `merged` wins over `needs_sync` because merged rows intentionally
    skip sync-status rendering — the branch is done, there is nothing
    to resync. Merged rows fall back to the plain `├── / └── ` form
    when `--icons=false` to avoid the single-width `■┄┆` glyph.
    """
    if merged:
        return "■┄┆ " if icons else ("└── " if is_last else "├── ")
    if icons and needs_sync and details != "none":
        return "└─✗ " if is_last else "├─✗ "
    return "└── " if is_last else "├── "


def classify(
    tracked: TrackedBranch,
    pr: PRState | None,
    status: NodeStatus | None,
    *,
    online: bool,
) -> IconKind:
    """Map a branch's state to its icon kind.

    Offline mode (no live review data) collapses to `· ○ ● ■`. Online
    mode unlocks `▼ ⚑ ✓ ◼` using the cached review flags populated by
    Stage 5's bulk fetch.
    """
    _ = tracked  # signature parity for future per-branch icon customizations
    if pr is None:
        if status is not None and not status.has_upstream:
            return "local_only"
        return "no_pr"
    if pr.merged:
        return "merged"
    return _review_icon(pr) if online else "pr_open"


def _review_icon(pr: PRState) -> IconKind:
    if pr.is_draft:
        return "pr_draft"
    if pr.is_approved and pr.has_open_comments:
        return "pr_approved_comments"
    if pr.is_approved:
        return "pr_approved"
    if pr.has_open_comments:
        return "pr_open_comments"
    return "pr_open"


def pr_icon(pr: PRState) -> tuple[str, str | None]:
    """Return `(symbol, color)` for a cached PR, using the tree-renderer palette.

    Thinner version of `classify` + `_review_icon` for call sites that
    only have a `PRState` (no `TrackedBranch` / `NodeStatus`) — e.g.
    `pm project status`'s flat worktree table.
    """
    if pr.merged:
        kind: IconKind = "merged"
    else:
        kind = _review_icon(pr)
    return _SYMBOL[kind], _ICON_COLOR[kind]


def _status_symbol(
    tracked: TrackedBranch | None,
    pr: PRState | None,
    status: NodeStatus | None,
    gctx: GraphCtx,
) -> str:
    """Icon + trailing space, or empty string when icons disabled.

    Merged rows already show `■` in the connector, so we skip the symbol
    here to avoid doubling up. `--details none` also elides it to keep
    the bare tree view uncluttered.
    """
    if (
        tracked is None
        or status is None
        or not gctx.render_opts.icons
        or gctx.details == "none"
        or status.merged
    ):
        return ""
    kind = classify(tracked, pr, status, online=gctx.render_opts.online)
    symbol = _SYMBOL[kind]
    if gctx.render_opts.color_mode != "off":
        color = _ICON_COLOR[kind]
        if color is not None:
            symbol = fmt.style(symbol, fg=color)
    return f"{symbol} "


def _name_label(
    tracked: TrackedBranch | None,
    pr: PRState | None,
    status: NodeStatus | None,
    pos: GraphPos,
    gctx: GraphCtx,
) -> str:
    """Styled branch name per color-mode.

    Current branch is always green+bold so the cwd row stands out
    regardless of mode. Non-current names:
      - `off` / `icon`: plain (bold for tracked rows)
      - `title` / `full`: colored per status when the status has a
        review-state color (`pr_approved` → green, `pr_open_comments` →
        red, etc.); otherwise plain bold
    """
    label = selectors.selector_for(gctx.repo_name, pos.branch)
    is_current = status is not None and status.is_current
    bold = is_current or not pos.implicit
    mode = gctx.render_opts.color_mode
    if is_current and mode != "off":
        return fmt.style(label, fg="green", bold=True)
    if mode in ("title", "full") and tracked is not None and status is not None:
        kind = classify(tracked, pr, status, online=gctx.render_opts.online)
        color = _ICON_COLOR[kind]
        if color is not None:
            return fmt.style(label, fg=color, bold=bold)
    return fmt.style(label, bold=bold)


def _apply_merged_style(text: str, style: MergedStyle) -> str:
    """Wrap the non-connector portion of a merged row in its dim/strike style.

    `text` here is a concatenation of already-styled fragments (contains
    markup), so we build the outer tag directly rather than going through
    `fmt.style` — that would escape the inner markup and emit the tag
    characters literally.
    """
    if style == "dimmed":
        return f"[dim]{text}[/]"
    if style == "strikethrough":
        return f"[strike]{text}[/]"
    return text


def _row_tail(
    tracked: TrackedBranch,
    pr: PRState | None,
    status: NodeStatus | None,
    pos: GraphPos,
    gctx: GraphCtx,
) -> str:
    """Everything after the branch label: commit group, dirty, suffix, current."""
    parts: list[str] = []
    merged = status is not None and status.merged
    details = gctx.details
    # Merged branches skip the commit group and dirty marker: they're
    # post-lifecycle, and showing "3 commits unpushed" on a merged row
    # is misleading. Matches gitstack's merged-row rendering.
    if not merged:
        group = _commit_group(status, details=details) if status else ""
        if group:
            parts.append(group)
        if status and status.dirty and details != "none":
            parts.append(fmt.style("[dirty]", fg="red", bold=True))
    parts.append(_suffix_token(tracked, pr, status))
    project = gctx.branch_projects.get(tracked.branch)
    if project is not None:
        parts.append(fmt.style(f"[{project}]", fg="blue", bold=True))
    wt_label = gctx.wt_labels.get((tracked.repo_name, tracked.branch))
    if wt_label is not None:
        parts.append(f"({wt_label})")
    elif status and status.is_current:
        parts.append("(current)")
    _ = pos  # reserved for future color-mode-full line wrapping
    return " " + " ".join(parts) if parts else ""


def _implicit_root_tail(pos: GraphPos) -> str:
    return " " + fmt.style("[untracked root]", fg="yellow") if pos.implicit else ""


def _commit_group(status: NodeStatus | None, *, details: Details) -> str:
    """Compact `(!N↑)` group replacing the old `[synced]`/`[N commits]` suffixes.

    `!` marks a branch that needs sync with its parent (details >= status).
    `N` is the branch commit count off `managed_base`; the trailing `↑` is
    shown when at least one commit is unpushed (no upstream, or
    `upstream..HEAD` is non-empty). Empty parens are elided entirely.
    Counts require details >= status-counts.
    """
    if status is None or details == "none":
        return ""
    pieces: list[str] = []
    if not status.synced:
        pieces.append("!")
    if details in ("status-counts", "all") and status.commit_count > 0:
        arrow = "↑" if (not status.has_upstream or status.ahead_of_remote > 0) else ""
        pieces.append(f"{status.commit_count}{arrow}")
    return f"({''.join(pieces)})" if pieces else ""


def _suffix_token(
    tracked: TrackedBranch,
    pr: PRState | None,
    status: NodeStatus | None,
) -> str:
    """Always-on `[MERGED] | [<pr_url>] | [REMOTE] | [LOCAL]` token.

    Precedence top-down, driven by the pr_state cache rather than any
    field on `TrackedBranch`.
    """
    _ = tracked  # kept in the signature for future per-branch styling
    if pr is not None and pr.merged:
        return fmt.style("[MERGED]", fg="magenta", bold=True)
    if pr is not None:
        return fmt.style(f"[{pr.pr_url}]", fg="magenta")
    if status and status.has_upstream:
        return fmt.style("[REMOTE]", fg="cyan")
    return fmt.style("[LOCAL]", fg="yellow")


def build_node_status(
    ctx: StackerCtx,
    gctx: GraphCtx,
    tracked: TrackedBranch,
    *,
    pr: PRState | None,
    details: Details,
) -> NodeStatus:
    """Consolidate every per-row fact the renderer might consult.

    Skips costly lookups when the detail level can't render them:
    commit counts only at `status-counts+`, dirty/upstream/remote-ahead
    only at `status+`. Synced and `is_current` are always computed
    because they cost ≤ 1 cheap git call and simplify branching.
    """
    synced = is_synced(ctx, tracked)
    is_current = gctx.current_branch == tracked.branch
    merged = pr is not None and pr.merged
    worktree = gctx.live.get(tracked.branch)
    wt_path = worktree.path if worktree is not None else None
    dirty = (
        has_graph_blocking_changes(wt_path) if wt_path is not None and details != "none"
        else False
    )
    if details == "none" or wt_path is None:
        return NodeStatus(
            synced=synced,
            commit_count=0,
            ahead_of_remote=0,
            has_upstream=False,
            dirty=False,
            is_current=is_current,
            merged=merged,
        )
    has_upstream = git.upstream_branch_name(wt_path) is not None
    ahead_of_remote = (
        count_ahead_of_remote(wt_path) if has_upstream else 0
    )
    commit_count = 0
    if details in ("status-counts", "all"):
        commit_count = _count_branch_commits(wt_path, tracked) or 0
    return NodeStatus(
        synced=synced,
        commit_count=commit_count,
        ahead_of_remote=ahead_of_remote,
        has_upstream=has_upstream,
        dirty=dirty,
        is_current=is_current,
        merged=merged,
    )


def count_ahead_of_remote(worktree_path: Path) -> int:
    """`rev-list --count <upstream>..HEAD`, or 0 when no upstream / on error."""
    upstream = git.upstream_branch(worktree_path)
    if not upstream:
        return 0
    with contextlib.suppress(git.GitError):
        return git.rev_count(worktree_path, f"{upstream}..HEAD")
    return 0


def _count_branch_commits(worktree_path: Path, tracked: TrackedBranch) -> int | None:
    with contextlib.suppress(git.GitError):
        return git.rev_count(worktree_path, f"{tracked.managed_base_commit}..HEAD")
    return None


def count_branch_commits(ctx: StackerCtx, tracked: TrackedBranch) -> int | None:
    path = locate.locate_worktree(ctx.paths, tracked.repo_name, tracked.branch)
    if path is None:
        return None
    return _count_branch_commits(path, tracked)


def is_synced(ctx: StackerCtx, tracked: TrackedBranch) -> bool:
    try:
        parent_head = git.rev_parse(
            ctx.paths.repo(tracked.parent_repo_name), tracked.parent_branch
        )
    except git.GitError:
        return False
    return parent_head == tracked.managed_base_commit


def has_graph_blocking_changes(path: Path) -> bool:
    with contextlib.suppress(git.GitError):
        return git.has_tracked_changes(path)
    return False


def ensure_syncable(path: Path) -> None:
    if git.has_tracked_changes(path):
        raise git.GitError(
            f"Tracked changes present in {path}. Commit or discard them before syncing."
        )
    if git.cherry_pick_in_progress(path):
        raise git.GitError(
            f"Cherry-pick already in progress in {path}. Resolve it before starting "
            "another sync."
        )


def render_legend(render_opts: RenderOptions) -> str:
    """Compact legend for icons, commit groups, suffixes, and tree decorators.

    Matches the active `render_opts` so icons only appear when they'd
    appear in the tree (`--icons`), and online-only glyphs only show up
    under `--online`. Color-per-status applies under the same rules
    `_status_symbol` uses, so the legend glyph looks identical to the
    one in the tree below it.
    """
    lines: list[str] = ["Legend:"]
    if render_opts.icons:
        lines.append(
            "  Icons:   " + "  ".join(
                _legend_icon(kind, render_opts) for kind in _OFFLINE_ICONS
            ),
        )
        if render_opts.online:
            lines.append(
                "  Online:  " + "  ".join(
                    _legend_icon(kind, render_opts) for kind in _ONLINE_ICONS
                ),
            )
    lines.append("  Group:   (!) needs sync   (N↑) N unpushed   (!N↑) both")
    lines.append("  Suffix:  [LOCAL]  [REMOTE]  [<url>]  [MERGED]  [dirty]")
    lines.append("  Tree:    ├─✗/└─✗ needs sync   ■┄┆ merged   > … (current)")
    return "\n".join(lines)


def _legend_icon(kind: IconKind, render_opts: RenderOptions) -> str:
    symbol = _SYMBOL[kind]
    if render_opts.color_mode != "off":
        color = _ICON_COLOR[kind]
        if color is not None:
            symbol = fmt.style(symbol, fg=color)
    return f"{symbol} {_ICON_MEANING[kind]}"
