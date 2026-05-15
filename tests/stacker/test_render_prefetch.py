"""End-to-end guard for the prefetch → render pipeline.

Today's render flow runs through `stacker/render/prefetch.py` — every
per-repo and per-node git fact is computed concurrently before
`render_graph_node` walks the tree. These tests dirty one worktree in a
real four-branch stack and assert the `[dirty]` marker lands on exactly
that row: proves the prefetch runs, the `dirty_by_path` / NodeStatus
cache is wired into `GraphCtx`, and the JSON path also consumes the
same cache.
"""

from __future__ import annotations

from project_manager.stacker.render.ls import LsOptions
from project_manager.stacker.service import StackerService

from .conftest import TrackedStack


def _line_for_branch(out: str, repo: str, branch: str) -> str | None:
    """Find the rendered row for `<repo>:<branch>` in `ls_text` output.

    The renderer prefixes branch names with `<repo>:` inside a rich style
    tag (`[bold]demo:b[/]`), so the disambiguating anchor is the
    `<repo>:<branch>` suffix, not the bare branch name.
    """
    token = f"{repo}:{branch}"
    return next((line for line in out.splitlines() if token in line), None)


def test_ls_text_prefetch_surfaces_dirty_marker(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    # Dirty branch `b`: touch a tracked file so `git status` shows
    # modified content. No commit — stacker's tracked-changes check
    # deliberately treats a modified tracked file as dirty.
    slot_b = tracked_stack.slots["b"]
    (slot_b.path / "b.txt").write_text("modified\n")

    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(details="status-counts"),
    )
    assert "[dirty]" in out, out
    line_b = _line_for_branch(out, tracked_stack.repo_name, "b")
    assert line_b is not None, out
    assert "[dirty]" in line_b, line_b
    # Other branches in the same stack must not carry the marker —
    # proves the prefetch is keyed correctly per worktree rather than
    # a spurious "one dirty → all dirty" fan.
    for other in ("a", "c", "d"):
        line = _line_for_branch(out, tracked_stack.repo_name, other)
        assert line is not None, (other, out)
        assert "[dirty]" not in line, (other, line)


def test_ls_text_prefetch_clean_stack_has_no_dirty_marker(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    # Sanity check: the prefetch's `dirty=False` default must not spuriously
    # paint every row dirty, and `git status` on a clean worktree must
    # continue to return empty output post-collapse.
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(details="status-counts"),
    )
    assert "[dirty]" not in out


def test_ls_json_prefetch_populates_commit_counts(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`_ls_json` goes through the same prefetch — its status-counts
    output should report each branch's commit_count.

    JSON does not surface `dirty` (by design, matches gitstack), so we
    assert the field the JSON path *does* expose. Each branch in the
    fixture has exactly one commit off its parent, so `commit_count==1`
    is the right empirical anchor that the prefetch ran end-to-end.
    """
    import json

    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(details="status-counts", json_output=True),
    )
    payload = json.loads(out)

    def _walk(nodes: list[dict]) -> dict[str, dict]:
        flat: dict[str, dict] = {}
        for n in nodes:
            flat[n["branch"]] = n
            flat.update(_walk(n.get("children", [])))
        return flat

    by_branch = _walk(payload["branches"])
    for name in ("a", "b", "c", "d"):
        assert by_branch[name]["commit_count"] == 1, (name, by_branch[name])
