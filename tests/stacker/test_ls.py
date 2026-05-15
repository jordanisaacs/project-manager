from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_manager.paths import Paths
from project_manager.pool.db import PoolDB
from project_manager.stacker import gh
from project_manager.stacker.db import StackerDB
from project_manager.stacker.models import PRState
from project_manager.stacker.render.ls import LsOptions, RenderOptions
from project_manager.stacker.service import StackerService

from .conftest import TrackedStack, commit_file
from .fakes import RecordingPRBackend


def test_ls_default_shows_all_tracked_branches(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    out = service.ls_text(tracked_stack.repo_name)
    for name in ("a", "b", "c", "d"):
        assert name in out


def test_ls_current_scope_narrows_to_lineage(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(target_branch="b", scope="current"),
    )
    for name in ("a", "b", "c", "d"):
        assert name in out


def test_ls_current_scope_includes_full_multi_arm_component(
    multi_arm_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`ls --scope current` from one arm must show every branch in the component.

    Tree: `main → a → {b1 → c1; b2 → c2}`. Running `ls -c` from `c1`
    must list both arms — `b2` and `c2` are siblings/cousins of the
    spine, not unrelated branches. Currently they're hidden because
    `lineage(c1)` only walks ancestors plus descendants-of-c1, so the
    "current stack" view drops half the work the user has in flight.
    """
    out = service.ls_text(
        multi_arm_stack.repo_name,
        LsOptions(target_branch="c1", scope="current"),
    )
    for name in ("a", "b1", "c1", "b2", "c2"):
        assert name in out, f"`ls -c` from c1 should include {name}; got:\n{out}"


def test_ls_details_none_strips_suffixes(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`--details none` emits bare names + the always-on LOCAL/REMOTE/URL/MERGED
    token, but no commit group, no dirty flag, and no `✗` sync marker.
    """
    out = service.ls_text(tracked_stack.repo_name, LsOptions(details="none"))
    assert "(!)" not in out
    assert "↑" not in out
    assert "[dirty]" not in out
    assert "✗" not in out


def test_ls_details_status_counts_includes_commit_counts(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(details="status-counts"),
    )
    # Each branch has 1 commit since its managed_base; no upstream, so the
    # whole count is `unpushed` → `↑` arrow shows.
    assert "(1↑)" in out


def test_ls_suffix_is_local_when_no_upstream_or_pr(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    out = service.ls_text(tracked_stack.repo_name)
    # The fixture never pushes branches, so every row ends in `[LOCAL]`.
    assert "[LOCAL]" in out
    assert "[REMOTE]" not in out


def _walk_branches(node: dict) -> list[dict]:
    out = [node]
    for child in node.get("children", []):
        out.extend(_walk_branches(child))
    return out


def _all_branches(payload: dict) -> list[dict]:
    out: list[dict] = []
    for root in payload["branches"]:
        out.extend(_walk_branches(root))
    return out


def test_ls_json_is_a_tree(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    raw = service.ls_text(tracked_stack.repo_name, LsOptions(json_output=True))
    payload = json.loads(raw)
    assert payload["current_branch"] is None
    # Single root `a` in the fixture (main is implicit, not tracked).
    assert len(payload["branches"]) == 1
    root = payload["branches"][0]
    assert root["branch"] == "a"
    assert root["is_root"] is True
    names = {b["branch"] for b in _all_branches(payload)}
    assert names == {"a", "b", "c", "d"}
    # Default details=status-counts → commit_count populated per node.
    for b in _all_branches(payload):
        assert b["commit_count"] == 1


def test_ls_json_current_branch_bubbles_from_options(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    raw = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(json_output=True, current=(tracked_stack.repo_name, "b")),
    )
    payload = json.loads(raw)
    assert payload["current_branch"] == "b"


def test_ls_json_details_none_omits_status_fields(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    raw = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(details="none", json_output=True),
    )
    payload = json.loads(raw)
    for b in _all_branches(payload):
        assert b["status"] is None
        assert b["needs_sync"] is None
        assert b["commit_count"] is None


def test_ls_json_details_all_includes_debug_fields(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    raw = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(details="all", json_output=True),
    )
    payload = json.loads(raw)
    for b in _all_branches(payload):
        assert "managed_base" in b
        assert "last_synced_parent_commit" in b
        assert "last_clean_head" in b


@pytest.mark.usefixtures("stacker_repo")
def test_ls_empty_repo_has_no_tracked_branches(
    service: StackerService,
) -> None:
    assert service.ls_text("demo") == "No tracked branches."


def test_ls_unsynced_branch_renders_bang_and_cross_connector(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """Advancing `a` past `b.managed_base_commit` makes `b` need a sync.

    The renderer should show `(!)`/`(!N↑)` in the commit group AND swap the
    connector leading into `b` for `├─✗` / `└─✗`.
    """
    commit_file(
        tracked_stack.slots["a"].path,
        "a_extra.txt",
        "a2\n",
        "a: second commit",
    )
    out = service.ls_text(tracked_stack.repo_name, LsOptions(details="status"))
    assert "!" in out
    assert "✗" in out


def test_ls_icons_off_suppresses_cross_connector(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    commit_file(
        tracked_stack.slots["a"].path,
        "a_extra.txt",
        "a2\n",
        "a: second commit",
    )
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(details="status", render=RenderOptions(icons=False)),
    )
    # `!` in the commit group still shows (it's semantic, not a connector).
    assert "(!" in out
    # Tree connector never renders ✗ when icons are disabled.
    assert "✗" not in out


def test_ls_current_marker_renders_when_current_provided(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(current=(tracked_stack.repo_name, "b")),
    )
    # `>` prefix on the current row, `(current)` suffix, only on branch `b`.
    assert "> " in out
    assert "(current)" in out
    # Non-current rows still get the 2-space gutter (repo header uses it too).
    assert "  [bold blue]demo[/]" in out


def test_ls_wt_labels_override_current_marker(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """With a wt_label for the cwd branch, (<wt>) replaces (current)."""
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(
            current=(tracked_stack.repo_name, "b"),
            wt_labels={(tracked_stack.repo_name, "b"): "my-wt"},
        ),
    )
    assert "(my-wt)" in out
    assert "(current)" not in out


def test_ls_wt_labels_mark_non_cwd_branches(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """wt_labels label sibling worktrees too (no cwd → no (current))."""
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(
            wt_labels={
                (tracked_stack.repo_name, "b"): "wt-b",
                (tracked_stack.repo_name, "c"): "wt-c",
            },
        ),
    )
    assert "(wt-b)" in out
    assert "(wt-c)" in out
    # Without `current`, the fallback marker never fires.
    assert "(current)" not in out


def test_ls_wt_labels_do_not_cross_repos(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """A label keyed on another repo must not bleed into this repo's tree."""
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(
            current=(tracked_stack.repo_name, "b"),
            wt_labels={("other-repo", "b"): "other-wt"},
        ),
    )
    # Same branch name `b`, different repo — label must not apply here.
    assert "(other-wt)" not in out
    # Fallback is the generic `(current)` marker.
    assert "(current)" in out


def test_ls_no_banner_when_current_branch_is_tracked(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """The `>` / `(current)` row-level markers carry the tracked-branch
    indication on their own; the banner is reserved for the untracked case.
    """
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(current=(tracked_stack.repo_name, "b")),
    )
    assert "not tracked by pm" not in out
    assert "(on tracked branch" not in out


def test_ls_banner_when_current_branch_is_not_tracked(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(current=(tracked_stack.repo_name, "scratch-branch")),
    )
    assert "(current branch `scratch-branch` is not tracked by pm)" in out


def test_ls_no_banner_when_cwd_outside_a_slot(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    out = service.ls_text(tracked_stack.repo_name, LsOptions(current=None))
    assert "not tracked by pm" not in out
    assert "(on tracked branch" not in out


def test_ls_shows_project_tag_on_every_row_in_project_slots(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """Every branch that lives in a project-owned pm slot gets a
    `[<project>]` token on its row — not just the current one — so the
    reader can navigate to any branch's slot.
    """
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(current=(tracked_stack.repo_name, "b")),
    )
    # The fixture claims all four slots for project "tracked-stack".
    branch_lines = [
        line
        for line in out.splitlines()
        if any(f"demo:{name}" in line for name in ("a", "b", "c", "d"))
    ]
    assert len(branch_lines) == 4
    for line in branch_lines:
        assert "[tracked-stack]" in line, line
    # On the current row the tag sits immediately before `(current)`.
    current_line = next(line for line in branch_lines if "(current)" in line)
    assert current_line.index("[tracked-stack]") < current_line.index("(current)")


def test_ls_omits_project_tag_when_slot_not_project_owned(
    pm_env: Paths,
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """A branch whose slot is unclaimed (or STACKER-owned) loses the
    `[<project>]` tag; other branches in project slots keep theirs.
    """
    # Release slot for branch "b" from the pool db — the live worktree
    # still exists, but the pool db no longer reports a project owner.
    pooldb = PoolDB(pm_env.pool_db())
    pooldb.release(tracked_stack.repo_name, tracked_stack.slots["b"].uuid)
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(current=(tracked_stack.repo_name, "b")),
    )
    b_line = next(line for line in out.splitlines() if "demo:b" in line)
    assert "[tracked-stack]" not in b_line
    # Siblings still in project slots keep the tag.
    a_line = next(line for line in out.splitlines() if "demo:a" in line)
    assert "[tracked-stack]" in a_line


def test_ls_legend_appended_in_text_mode(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    out = service.ls_text(tracked_stack.repo_name, LsOptions(legend=True))
    # Tree renders first; legend is a trailing reference block.
    assert "demo" in out.split("Legend:")[0]
    assert "Legend:" in out
    # Offline icons are always in the legend; each name → glyph pair.
    for glyph, word in (("·", "local only"), ("○", "pushed"), ("●", "PR open")):
        assert glyph in out
        assert word in out


def test_ls_legend_hides_icon_row_when_icons_off(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(legend=True, render=RenderOptions(icons=False)),
    )
    assert "Legend:" in out
    # Icon rows elided; commit-group + suffix sections stay.
    legend = out.split("Legend:", 1)[1]
    assert "Icons:" not in legend
    assert "Group:" in legend
    assert "Suffix:" in legend


def test_ls_legend_includes_online_row_only_under_online(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    on = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(legend=True, render=RenderOptions(online=True)),
    )
    off = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(legend=True, render=RenderOptions(online=False)),
    )
    assert "Online:" in on
    assert "Online:" not in off


def test_ls_legend_ignored_in_json_mode(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    raw = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(legend=True, json_output=True),
    )
    # Must still parse as JSON — no "Legend:" preamble.
    payload = json.loads(raw)
    assert "branches" in payload


# --- color tests -----------------------------------------------------------

# `ls_text` now emits rich markup; the CLI resolves it via Console.print.
# Tests assert on markup patterns rather than raw ANSI bytes — rich handles
# TTY/NO_COLOR gating at print time.


def _strip_markup(s: str) -> str:
    """Render markup to plain text (no ANSI) for content-only assertions."""
    import io

    from rich.console import Console

    buf = io.StringIO()
    Console(
        file=buf, force_terminal=False, no_color=True, width=120, highlight=False, markup=False
    ).print(
        s,
        markup=True,
        highlight=False,
    )
    return buf.getvalue()


def _render_ansi(s: str) -> str:
    """Render markup through a forced-TTY Console for ANSI assertions."""
    import io

    from rich.console import Console

    buf = io.StringIO()
    Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        width=120,
        highlight=False,
        markup=False,
    ).print(
        s,
        markup=True,
        highlight=False,
    )
    return buf.getvalue()


def _seed_approved(
    svc: StackerService,
    repo_name: str,
    branch: str,
) -> RecordingPRBackend:
    """Install an approved PR for `branch` via a fake PR backend.

    Returns the backend so callers can further tweak it. The PR state is
    pre-cached so the `--online` refresh path and the offline path both
    see the same approval flags after the bulk fetch runs.
    """
    backend = svc._ctx.pr_backend
    assert isinstance(backend, RecordingPRBackend)
    svc.db.upsert_pr_state(
        PRState(
            repo_name=repo_name,
            branch=branch,
            pr_url="https://github.com/acme/widgets/pull/1",
            state="OPEN",
            is_approved=True,
        ),
    )
    backend.review_by_pr[("acme", "widgets", 1)] = gh.PRReviewSummary(
        state="OPEN",
        is_draft=False,
        is_approved=True,
        has_open_comments=False,
    )
    return backend


def test_ls_icon_mode_does_not_color_non_current_branch_names(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """Regression: under the default `icon` color mode only the status
    symbol should carry a per-row color. Previously every tracked branch
    name came out green+bold from a stale fallback.
    """
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(
            current=(tracked_stack.repo_name, "b"),
            render=RenderOptions(online=False, color_mode="icon"),
        ),
    )
    # Current row: green+bold (the cwd marker still stands out).
    assert "[bold green]demo:b[/]" in out
    # Non-current rows: no green styling on the name.
    assert "[bold green]demo:a" not in out
    assert "[bold green]demo:c" not in out


def test_ls_icon_mode_colors_symbol_not_name_for_approved(
    tracked_stack: TrackedStack,
    pm_env: Paths,
) -> None:
    svc = StackerService(
        StackerDB(pm_env.stacker_db()),
        pm_env,
        pr_backend=RecordingPRBackend(),
    )
    _seed_approved(svc, tracked_stack.repo_name, "b")
    out = svc.ls_text(
        tracked_stack.repo_name,
        LsOptions(render=RenderOptions(online=True, color_mode="icon")),
    )
    # Approved glyph is green.
    assert "[green]✓[/]" in out
    # Branch name not green under `icon` mode.
    assert "[bold green]demo:b" not in out


def test_ls_title_mode_colors_both_symbol_and_approved_branch_name(
    tracked_stack: TrackedStack,
    pm_env: Paths,
) -> None:
    svc = StackerService(
        StackerDB(pm_env.stacker_db()),
        pm_env,
        pr_backend=RecordingPRBackend(),
    )
    _seed_approved(svc, tracked_stack.repo_name, "b")
    out = svc.ls_text(
        tracked_stack.repo_name,
        LsOptions(render=RenderOptions(online=True, color_mode="title")),
    )
    assert "[green]✓[/]" in out
    assert "[bold green]demo:b[/]" in out


def test_ls_color_mode_off_drops_current_branch_fg(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """`--color-mode off` drops per-status / current fg on names and icons,
    but `fmt.style`-based chrome (repo header, suffix tokens, etc.) keeps
    its colors.
    """
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(
            current=(tracked_stack.repo_name, "b"),
            render=RenderOptions(color_mode="off"),
        ),
    )
    # Current row is bold but not green.
    assert "[bold]demo:b[/]" in out
    assert "[bold green]demo:b" not in out
    # Repo header keeps its blue styling (unaffected by color_mode).
    assert "[bold blue]demo[/]" in out


def test_ls_renders_without_ansi_when_printed_to_non_terminal(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """The `ls_text` output carries rich markup; when printed via a non-TTY
    Console (the default when stdout is piped) the ANSI escape codes drop
    out. NO_COLOR is honored by rich's Console natively.
    """
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(current=(tracked_stack.repo_name, "b")),
    )
    rendered = _strip_markup(out)
    assert "\x1b[" not in rendered
    # Content survives: branch names and tree chars.
    assert "demo:b" in rendered


def test_ls_renders_ansi_when_printed_to_terminal(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    """Forced-TTY render resolves markup to ANSI — end-to-end sanity that
    the same markup string covers both paths.
    """
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(current=(tracked_stack.repo_name, "b")),
    )
    rendered = _render_ansi(out)
    assert "\x1b[" in rendered  # ANSI present
    assert "\x1b[1;32m" in rendered or "\x1b[32;1m" in rendered  # green+bold somewhere


# --- end color tests -------------------------------------------------------


def test_ls_empty_message_mentions_untracked_current(
    stacker_repo: tuple[str, Path],
    service: StackerService,
) -> None:
    """Empty repo + cwd in slot on untracked branch: surface both facts."""
    repo_name, _ = stacker_repo
    out = service.ls_text(
        repo_name,
        LsOptions(current=(repo_name, "exploration")),
    )
    assert repo_name in out
    assert "`exploration` is not tracked by pm" in out


def test_ls_local_only_icon_shown_by_default(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    out = service.ls_text(tracked_stack.repo_name, LsOptions(details="status"))
    assert "·" in out  # every branch in the fixture is local-only


def test_ls_icons_off_hides_status_symbols(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(details="status", render=RenderOptions(icons=False)),
    )
    for sym in ("·", "○", "●", "■"):
        assert sym not in out


def test_ls_merged_branch_uses_merged_connector_and_suffix(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    service.db.upsert_pr_state(
        PRState(
            repo_name=tracked_stack.repo_name,
            branch="c",
            pr_url="https://github.com/acme/widgets/pull/7",
            state="MERGED",
            merged=True,
        ),
    )
    out = service.ls_text(tracked_stack.repo_name, LsOptions(details="status"))
    assert "[MERGED]" in out
    assert "■┄┆" in out


def test_ls_hide_merged_drops_merged_rows(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    service.db.upsert_pr_state(
        PRState(
            repo_name=tracked_stack.repo_name,
            branch="c",
            pr_url="https://github.com/acme/widgets/pull/7",
            state="MERGED",
            merged=True,
        ),
    )
    out = service.ls_text(
        tracked_stack.repo_name,
        LsOptions(render=RenderOptions(hide_merged=True)),
    )
    # `c` should be filtered; its children (`d`) stay — they re-root at
    # the now-implicit `c` slot.
    assert "c" not in out or "[MERGED]" not in out
    assert "demo:d" in out


def test_ls_pr_url_suffix_and_open_icon_when_pr_state_present(
    tracked_stack: TrackedStack,
    service: StackerService,
) -> None:
    service.db.upsert_pr_state(
        PRState(
            repo_name=tracked_stack.repo_name,
            branch="b",
            pr_url="https://github.com/acme/widgets/pull/42",
            state="OPEN",
        ),
    )
    out = service.ls_text(tracked_stack.repo_name, LsOptions(details="status"))
    assert "https://github.com/acme/widgets/pull/42" in out
    # Open PR with no review data → `●`. `·` still present for the
    # other local-only branches, but `●` should appear at least once.
    assert "●" in out


def test_ls_online_refresh_writes_review_fields_to_pr_state(
    tracked_stack: TrackedStack,
    pm_env: Paths,
) -> None:
    """--online refreshes pr_state via one batch GraphQL call per repo.

    Seed `b` with a cached URL, stub the backend to return approved +
    comments, and verify the upgraded icon plus the DB write-back.
    """
    backend = RecordingPRBackend()
    svc = StackerService(StackerDB(pm_env.stacker_db()), pm_env, pr_backend=backend)
    svc.db.upsert_pr_state(
        PRState(
            repo_name=tracked_stack.repo_name,
            branch="b",
            pr_url="https://github.com/acme/widgets/pull/42",
            state="OPEN",
        ),
    )
    backend.review_by_pr[("acme", "widgets", 42)] = gh.PRReviewSummary(
        state="OPEN",
        is_draft=False,
        is_approved=True,
        has_open_comments=True,
    )
    out = svc.ls_text(tracked_stack.repo_name, LsOptions(details="status"))
    # Approved + comments combo → ◼ icon (gitstack's `pr_approved_comments`).
    assert "◼" in out
    assert backend.review_calls == [[("acme", "widgets", 42)]]
    refreshed = svc.db.get_pr_state(tracked_stack.repo_name, "b")
    assert refreshed is not None
    assert refreshed.is_approved is True
    assert refreshed.has_open_comments is True


def test_ls_online_false_makes_zero_github_calls(
    tracked_stack: TrackedStack,
    pm_env: Paths,
) -> None:
    backend = RecordingPRBackend()
    svc = StackerService(StackerDB(pm_env.stacker_db()), pm_env, pr_backend=backend)
    svc.db.upsert_pr_state(
        PRState(
            repo_name=tracked_stack.repo_name,
            branch="b",
            pr_url="https://github.com/acme/widgets/pull/42",
            state="OPEN",
        ),
    )
    svc.ls_text(
        tracked_stack.repo_name,
        LsOptions(render=RenderOptions(online=False)),
    )
    assert backend.review_calls == []
