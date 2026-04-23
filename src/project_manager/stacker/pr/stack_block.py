from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING
from urllib.parse import quote

from project_manager.stacker import gh
from project_manager.stacker.models import RepoPRConfig, TrackedBranch

if TYPE_CHECKING:
    from project_manager.stacker.ctx import StackerCtx


@dataclass(frozen=True)
class _StackRender:
    """Shared context threaded through stack-block rendering recursion."""

    component: list[TrackedBranch]
    pr_map: dict[str, gh.PullRequest]
    config: RepoPRConfig
    current_repo: gh.RepoInfo


def render_stack_block(
    ctx: StackerCtx,  # noqa: ARG001 — reserved for future render hooks; keeps signature consistent with other pr/ fns
    render_ctx: _StackRender,
    current_node: TrackedBranch,
) -> str:
    """Render the stack block embedded in a PR body.

    One-line preamble pointing reviewers at the current branch's files
    view, followed by an indented tree. Each branch renders as
    `[branch](pr) [[Files changed](files)]` with `[MERGED]` appended
    for landed PRs; the current branch's name is bolded inside the link.
    """
    lines = ["<!-- stacker:begin -->", "## Stacker", ""]
    # Preamble + Files-changed links only make sense when every PR bases
    # on trunk (repo-pr). In pr-pr mode GitHub's own base-chain already
    # gives reviewers the per-branch diff.
    if render_ctx.config.mode == "repo-pr":
        preamble = _files_url(current_node, render_ctx)
        if preamble is not None:
            lines.append(
                f"Use this [link]({preamble}) to review incremental changes."
            )
            lines.append("")
    component_keys = {node.branch for node in render_ctx.component}
    roots = [
        node for node in render_ctx.component if node.parent_branch not in component_keys
    ]
    for index, root in enumerate(sorted(roots, key=lambda item: item.branch)):
        if index:
            lines.append("")
        lines.extend(_render_stack_lines(render_ctx, root, current_node, prefix=""))
    lines.append("<!-- stacker:end -->")
    return "\n".join(lines)


def _render_stack_lines(
    render_ctx: _StackRender,
    node: TrackedBranch,
    current_node: TrackedBranch,
    *,
    prefix: str,
) -> list[str]:
    is_current = node.branch == current_node.branch
    label = f"**{node.branch}**" if is_current else node.branch
    pr = render_ctx.pr_map.get(node.branch)
    if pr:
        parts = [f"[{label}]({pr.url})"]
        if render_ctx.config.mode == "repo-pr":
            files_url = _files_url(node, render_ctx)
            if files_url:
                parts.append(f"[[Files changed]({files_url})]")
        # Surface merged/closed PRs with a label. Keeps history visible
        # instead of silently dropping landed PRs.
        if pr.state == "MERGED":
            parts.append("[MERGED]")
        elif pr.state == "CLOSED":
            parts.append("[CLOSED]")
        content = " ".join(parts)
    else:
        content = label
    lines = [f"{prefix}- {content}"]
    children = sorted(
        [child for child in render_ctx.component if child.parent_branch == node.branch],
        key=lambda item: item.branch,
    )
    for child in children:
        lines.extend(_render_stack_lines(render_ctx, child, current_node, prefix=prefix + "  "))
    return lines


def _files_url(node: TrackedBranch, render_ctx: _StackRender) -> str | None:
    """Build `<pr-url>/files/<base>..<head>` for reviewer navigation.

    Scoped to the PR so reviewers see only the commits unique to that
    branch. For a merged PR the range is dropped (`<pr>/files`) since
    the commit range no longer reflects reviewable changes. Uses
    `last_clean_head` (persisted by sync/init/repair), so ancestors
    and siblings render correctly even when not currently checked out.
    """
    pr = render_ctx.pr_map.get(node.branch)
    if pr is None:
        return None
    if pr.state == "MERGED":
        return f"{pr.url}/files"
    head_sha = node.last_clean_head
    if not head_sha:
        return None
    base_sha = node.managed_base_commit
    return (
        f"{pr.url}/files/"
        f"{quote(base_sha, safe=':/')}..{quote(head_sha, safe=':/')}"
    )


@contextlib.contextmanager
def body_file(body: str) -> Iterator[Path]:
    """Yield a path to a temp file holding `body`; unlinked on exit."""
    file = NamedTemporaryFile("w", delete=False, encoding="utf-8")  # noqa: SIM115
    try:
        file.write(body)
        file.flush()
        file.close()
        yield Path(file.name)
    finally:
        Path(file.name).unlink(missing_ok=True)
