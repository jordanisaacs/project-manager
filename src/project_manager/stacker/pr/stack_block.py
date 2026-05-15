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


# Format constants: header is matched as a substring on a line, footer
# is the first line whose strip() starts with at least nine dashes.
STACK_HEADER = "## 🥞 Stacked PR"
STACK_SEPARATOR = "---------"


@dataclass(frozen=True)
class _StackRender:
    """Shared context threaded through stack-block rendering recursion."""

    component: list[TrackedBranch]
    pr_map: dict[str, gh.PullRequest]
    config: RepoPRConfig
    current_repo: gh.RepoInfo
    # Live `git rev-parse <branch>` from the canonical repo, keyed by branch.
    # Lets `_files_url` render `<base>..<head>` against the SHA actually
    # pushed (or about to be) instead of the `last_clean_head` cached at
    # init/sync time, which goes stale the moment a local commit lands.
    live_heads: dict[str, str]


def render_stack_block(
    ctx: StackerCtx,  # noqa: ARG001 — reserved for future render hooks; keeps signature consistent with other pr/ fns
    render_ctx: _StackRender,
    current_node: TrackedBranch,
) -> str:
    """Render the gitstack-format leading block of a PR body.

    Layout matches ReviewStack's gitstack parser: a `## 🥞 Stacked PR`
    header, an indented branch tree, then a `---------` separator that
    `compose_body_with_block` uses to splice in the user content. Each
    branch renders as `[branch](pr) [[Files changed](files)]` with
    `[MERGED]`/`[CLOSED]` appended for landed/abandoned PRs; the current
    branch's name is bolded inside the link.

    Files-changed links are still gated to `repo-pr` mode — in pr-pr
    mode GitHub's own base-chain already gives reviewers the per-branch
    diff.
    """
    # Wrap the gitstack-format content in HTML-comment markers. The markers
    # are our authoritative managed-block boundary — `strip_managed_block`
    # parses them — because they're invisible in rendered markdown and
    # cannot collide with user content the way a `## 🥞` heading or
    # `---------` rule could. The inner header + separator are what
    # ReviewStack's gitstack parser keys off.
    lines = ["<!-- stacker:begin -->", STACK_HEADER, ""]
    component_keys = {node.branch for node in render_ctx.component}
    roots = [node for node in render_ctx.component if node.parent_branch not in component_keys]
    for index, root in enumerate(sorted(roots, key=lambda item: item.branch)):
        if index:
            lines.append("")
        lines.extend(_render_stack_lines(render_ctx, root, current_node, prefix=""))
    lines.append("")
    lines.append(STACK_SEPARATOR)
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
    """Build a Files-changed URL for reviewer navigation.

    The range form `<pr>/files/<base>..<head>` only renders when both
    SHAs are reachable in the PR's commit graph — true for branches
    nested under another tracked, non-merged branch (parent commits are
    part of the child's repo-pr commit list) but not for branches whose
    parent is the trunk. For root-of-stack branches the base SHA is a
    trunk commit outside the PR graph, so GitHub 404s; we emit
    `<pr>/files` instead, suppressing the range form in that case.
    A merged parent collapses to the same case (its commits land on
    trunk and the parent ref is gone). Head SHA prefers the live branch
    ref via `render_ctx.live_heads`, falling back to the `last_clean_head`
    cached by sync/init/repair.
    """
    pr = render_ctx.pr_map.get(node.branch)
    if pr is None:
        return None
    if pr.state == "MERGED":
        return f"{pr.url}/files"
    parent_pr = render_ctx.pr_map.get(node.parent_branch)
    parent_is_trunk = node.parent_branch == render_ctx.config.trunk_branch
    parent_is_merged = parent_pr is not None and parent_pr.state == "MERGED"
    if parent_is_trunk or parent_is_merged:
        return f"{pr.url}/files"
    head_sha = render_ctx.live_heads.get(node.branch) or node.last_clean_head
    if not head_sha:
        return None
    base_sha = node.managed_base_commit
    return f"{pr.url}/files/{quote(base_sha, safe=':/')}..{quote(head_sha, safe=':/')}"


@contextlib.contextmanager
def body_file(body: str) -> Iterator[Path]:
    """Yield a path to a temp file holding `body`; unlinked on exit."""
    with NamedTemporaryFile("w", encoding="utf-8") as f:
        f.write(body)
        f.flush()
        yield Path(f.name)
