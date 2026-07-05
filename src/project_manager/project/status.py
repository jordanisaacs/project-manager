"""Single-project status: per-worktree findings joined with branch + PR + stacker state."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import cast

from project_manager import check as check_mod
from project_manager.paths import Paths
from project_manager.project import db as project_db
from project_manager.project import discovery
from project_manager.render import Column, Section
from project_manager.stacker import git as stacker_git
from project_manager.stacker import locate as stacker_locate
from project_manager.stacker.ctx import StackerCtx
from project_manager.stacker.models import PRState, TrackedBranch
from project_manager.stacker.render import graph
from project_manager.stacker.render.ls import LsOptions, RenderOptions, arm_branch_set, ls_structure
from project_manager.stacker.render.ls import ls_text as render_ls_text


class StatusSection(StrEnum):
    """One slice of a project status report.

    Selectable via `pm project status --section`. `SESSIONS` is gathered
    by `agent.ls` (not this module) but lives in the same enum so the
    CLI parses one comma list. The other three are fields on
    `ProjectStatus`; `status()` skips work for any not requested.
    """

    WORKTREES = "worktrees"
    PRS = "prs"
    STACKER = "stacker"
    SESSIONS = "sessions"


ALL_STATUS_SECTIONS: frozenset[StatusSection] = frozenset(StatusSection)
# The subset that this module's `status()` actually populates; sessions
# are someone else's problem.
_PROJECT_SECTIONS: frozenset[StatusSection] = frozenset(
    {StatusSection.WORKTREES, StatusSection.PRS, StatusSection.STACKER},
)

_KIND_STYLE: dict[check_mod.Kind, str] = {
    check_mod.Kind.ACTIVE: "green",
    check_mod.Kind.DETACHED: "dim",
    check_mod.Kind.DRIFT: "yellow",
    check_mod.Kind.STALE: "red",
    check_mod.Kind.BROKEN: "red",
    check_mod.Kind.ORPHAN_FORWARD: "red",
    check_mod.Kind.ORPHAN_OWNER: "red",
    check_mod.Kind.OPS_OWNED: "cyan",
}


@dataclass(frozen=True)
class WorktreeRow:
    """One row in the worktree-status table.

    `branch` is the live checked-out branch for ACTIVE/DRIFT rows and
    the persisted `worktrees.branch` for DETACHED rows (restored on
    re-attach). Orphan and broken rows have no meaningful branch so
    `branch` is None.

    `pr` mirrors the stacker pr_state cache keyed on (repo, branch);
    None when no branch is known or no PR has been associated.

    `tracked` is True when `(repo, branch)` is in stacker's
    `tracked_branches` — the worktree's checked-out branch is part of
    a managed stack.
    """

    wt: str | None
    repo: str | None
    branch: str | None
    pr: PRState | None
    tracked: bool
    finding: check_mod.Finding

    def __pm_json__(self) -> dict:
        f = self.finding
        return {
            "wt": self.wt,
            "repo": self.repo,
            "branch": self.branch,
            "pr": _pr_to_dict(self.pr),
            "tracked": self.tracked,
            "kind": f.kind.value,
            "forward_path": str(f.forward_path) if f.forward_path else None,
            "slot_path": str(f.slot_path) if f.slot_path else None,
            "detail": f.detail,
        }


@dataclass(frozen=True)
class PRRow:
    """Flat per-worktree PR row, surfaced when any worktree has a cached PR."""

    wt: str
    repo: str
    branch: str
    pr: PRState

    def __pm_json__(self) -> dict:
        return {
            "wt": self.wt,
            "repo": self.repo,
            "branch": self.branch,
            "pr_url": self.pr.pr_url,
            "pr_number": self.pr.pr_number,
            "state": self.pr.state,
            "is_draft": self.pr.is_draft,
            "merged": self.pr.merged,
            "merged_at": self.pr.merged_at,
            "is_approved": self.pr.is_approved,
            "has_open_comments": self.pr.has_open_comments,
        }


@dataclass(frozen=True)
class StackerRow:
    """One row in the Stacker section — a per-repo tree of relevant arms.

    `info` is pre-rendered rich markup from the stacker tree renderer;
    the CLI table displays it with `Column(markup=True)` so the ANSI
    styling lands in the cell.
    """

    repo: str
    info: str
    branches: list[dict] = field(default_factory=list)
    current_branch: str | None = None
    operation: dict | None = None

    def __pm_json__(self) -> dict:
        return {
            "repo": self.repo,
            "info": self.info,
            "branches": self.branches,
            "current_branch": self.current_branch,
            "operation": self.operation,
        }


@dataclass(frozen=True)
class ProjectStatus:
    """Everything `pm project status` needs except sessions (which come from `agent.ls`)."""

    worktrees: list[WorktreeRow]
    prs: list[PRRow]
    stacker: list[StackerRow]


def _pr_to_dict(pr: PRState | None) -> dict | None:
    if pr is None:
        return None
    return {
        "pr_url": pr.pr_url,
        "pr_number": pr.pr_number,
        "state": pr.state,
        "is_draft": pr.is_draft,
        "merged": pr.merged,
        "merged_at": pr.merged_at,
        "is_approved": pr.is_approved,
        "has_open_comments": pr.has_open_comments,
    }


def _pr_cell(row: WorktreeRow) -> str:
    # Empty string falls through `Column.empty="-"` for no-PR rows so
    # the column stays uniform.
    if row.pr is None:
        return ""
    symbol, _ = graph.pr_icon(row.pr)
    return symbol


def _pr_style(row: WorktreeRow) -> str | None:
    if row.pr is None:
        return None
    _, color = graph.pr_icon(row.pr)
    return color


def _stacker_cell(row: WorktreeRow) -> str:
    return "✓" if row.tracked else ""


WORKTREE_COLUMNS: list[Column[WorktreeRow]] = [
    Column("Worktree", lambda r: r.wt or ""),
    Column(
        "Kind",
        lambda r: r.finding.kind.value,
        style=lambda r: _KIND_STYLE.get(r.finding.kind, ""),
    ),
    Column("Branch", lambda r: r.branch or ""),
    Column("PR", _pr_cell, style=_pr_style),
    Column("Stacker", _stacker_cell, style="green"),
    Column("Detail", lambda r: r.finding.detail),
]


def _check_mark(flag: bool) -> str:  # noqa: FBT001
    return "✓" if flag else ""


PR_COLUMNS: list[Column[PRRow]] = [
    Column(
        "Icon",
        lambda r: graph.pr_icon(r.pr)[0],
        style=lambda r: graph.pr_icon(r.pr)[1],
    ),
    Column("Worktree", "wt"),
    Column("Branch", "branch"),
    Column("State", lambda r: r.pr.state),
    Column("Draft", lambda r: _check_mark(r.pr.is_draft)),
    Column("Approved", lambda r: _check_mark(r.pr.is_approved)),
    Column("Comments", lambda r: _check_mark(r.pr.has_open_comments)),
    Column("URL", lambda r: r.pr.pr_url, style="magenta"),
]


# `info` carries a pre-rendered rich-markup tree — parse the markup
# inside the cell so colors/glyphs land correctly rather than showing
# as literal `[tag]` text.
STACKER_COLUMNS: list[Column[StackerRow]] = [
    Column("Repo", "repo", style="bold blue"),
    Column("Info", "info", markup=True),
]


_NO_REPO = "<no repo>"


def worktree_sections(rows: list[WorktreeRow]) -> list[Section[WorktreeRow]]:
    """Group worktree rows by repo (sorted), worktree name within each repo.

    Orphan-owner rows still know their `repo` from the pool db, so they
    bucket with the rest of that repo's rows. Rows with no repo at all
    (unusual) land under a trailing `<no repo>` section.
    """
    by_repo: dict[str, list[WorktreeRow]] = {}
    for row in rows:
        by_repo.setdefault(row.repo or _NO_REPO, []).append(row)
    out: list[Section[WorktreeRow]] = []
    for repo in sorted(by_repo):
        items = sorted(by_repo[repo], key=lambda r: (r.wt or "", r.finding.kind.value))
        out.append(Section(title=repo, rows=items))
    return out


def _live_branches(paths: Paths, repo: str) -> dict[Path, str]:
    """`{resolved_slot_path: branch}` for every live worktree in the pool repo.

    Resolves each path so downstream `slot_path` comparisons line up
    regardless of symlink form. Missing repos or failed git calls
    degrade to an empty dict so the gatherer keeps going.
    """
    repo_dir = paths.repo(repo)
    if not repo_dir.is_dir():
        return {}
    try:
        infos = stacker_git.worktree_list(repo_dir)
    except stacker_git.GitError:
        return {}
    out: dict[Path, str] = {}
    for info in infos:
        if info.branch is None:
            continue
        try:
            resolved = info.path.resolve()
        except OSError:
            resolved = info.path
        out[resolved] = info.branch
    return out


def _resolve_branch(
    finding: check_mod.Finding,
    repo_branches: dict[str, dict[Path, str]],
    paths: Paths,
    project: str,
) -> str | None:
    """Best-effort branch for a worktree row.

    ACTIVE/DRIFT rows have a live slot: consult `git worktree list` for
    the repo and match the slot path. DETACHED rows have no live
    checkout but keep the last-seen branch persisted on the project
    db's `worktrees.branch`. Everything else (BROKEN, STALE, ORPHAN_*,
    OPS_OWNED) has no branch to report.
    """
    if finding.repo is None:
        return None
    if finding.kind in (check_mod.Kind.ACTIVE, check_mod.Kind.DRIFT):
        if finding.slot_path is None:
            return None
        live = repo_branches.setdefault(
            finding.repo,
            _live_branches(paths, finding.repo),
        )
        try:
            resolved = finding.slot_path.resolve()
        except OSError:
            resolved = finding.slot_path
        return live.get(resolved)
    if finding.kind == check_mod.Kind.DETACHED and finding.wt is not None:
        db_path = paths.project_db(project)
        if not db_path.is_file():
            return None
        with project_db.readonly(db_path) as conn:
            return project_db.get_branch(conn, finding.wt)
    return None


def _find_root(ctx: StackerCtx, tracked: TrackedBranch) -> TrackedBranch:
    """Walk parent chain up to the first tracked branch with no tracked parent."""
    current = tracked
    while True:
        parent = ctx.db.get_branch(current.parent_repo_name, current.parent_branch)
        if parent is None:
            return current
        current = parent


def _current_position(paths: Paths) -> tuple[str, str] | None:
    """`(repo, branch)` of the cwd's pm slot, or None when not inside one."""
    slot = stacker_locate.slot_for_cwd(paths)
    if slot is None:
        return None
    try:
        branch = stacker_git.current_branch(slot.path)
    except stacker_git.GitError:
        return None
    if not branch:
        return None
    return slot.repo_name, branch


def _stacker_row_structured(
    ctx: StackerCtx,
    repo: str,
    roots: list[TrackedBranch],
    current: tuple[str, str] | None,
) -> StackerRow:
    """Structured `StackerRow` for one repo: the arm tree + paused-op state.

    The arm is the union of each root's lineage (`scope="current"`), the
    same selection the text renderer walks — so the structured tree
    matches the text arm. `info` is left empty: `--json` consumers read
    `branches`, and skipping the text render keeps this to one prefetch.
    """
    arm: list[TrackedBranch] = []
    seen: set[tuple[str, str]] = set()
    for root in roots:
        for b in arm_branch_set(ctx, repo, root.branch, "current"):
            key = (b.repo_name, b.branch)
            if key not in seen:
                seen.add(key)
                arm.append(b)
    tree = ls_structure(ctx, arm, "status-counts", current)
    op = ctx.db.get_operation(repo)
    return StackerRow(
        repo=repo,
        info="",
        branches=cast("list[dict]", tree["branches"]),
        current_branch=cast("str | None", tree["current_branch"]),
        operation=op.__pm_json__() if op is not None else None,
    )


def _gather_stacker(
    ctx: StackerCtx,
    paths: Paths,
    tracked_checked_out_per_repo: dict[str, list[TrackedBranch]],
    wt_labels: dict[tuple[str, str], str],
    *,
    structured: bool = False,
) -> list[StackerRow]:
    """Render the stacker arms for each repo with at least one tracked checkout.

    For every `(repo → [branches])` pair, find the unique roots of those
    branches' arms and render each root with the existing stacker tree
    (`scope="current"`, `target_branch=root`) — since a root has no
    tracked parent, the lineage expands into the root + all descendants,
    i.e. the "full arm". Multiple arms are rendered back-to-back.

    `wt_labels` maps `(repo, branch) → pm worktree name` for every
    worktree in the project; passed into `LsOptions.wt_labels` so the
    tree replaces `(current)` with `(<wt-name>)` for rows that belong
    to one of the project's worktrees.
    """
    current = _current_position(paths)
    render_opts = RenderOptions(online=False)
    rows: list[StackerRow] = []
    for repo in sorted(tracked_checked_out_per_repo):
        branches = tracked_checked_out_per_repo[repo]
        if not branches:
            continue
        roots: dict[str, TrackedBranch] = {}
        for b in branches:
            root = _find_root(ctx, b)
            roots.setdefault(root.branch, root)
        sorted_roots = [root for _, root in sorted(roots.items())]
        if structured:
            rows.append(_stacker_row_structured(ctx, repo, sorted_roots, current))
            continue
        segments = [
            render_ls_text(
                ctx,
                repo,
                LsOptions(
                    target_branch=root.branch,
                    scope="current",
                    current=current,
                    render=render_opts,
                    wt_labels=wt_labels,
                ),
            )
            for root in sorted_roots
        ]
        rows.append(StackerRow(repo=repo, info="\n".join(segments)))
    return rows


def status(
    paths: Paths,
    project: str,
    sections: frozenset[StatusSection] = ALL_STATUS_SECTIONS,
    *,
    stacker_structured: bool = False,
) -> ProjectStatus:
    """Gather worktree findings + branch + cached PR state + stacker arms.

    `sections` selects which slices to populate; unrequested fields are
    empty lists. Pass `frozenset({StatusSection.WORKTREES})` to skip the
    stacker tree render (the most expensive piece) when the caller only
    wants the worktree health table. With no project section requested,
    all heavy fanout is skipped and an empty `ProjectStatus` is returned
    — useful when the caller only wants sessions (gathered elsewhere).

    Raises `ProjectError` if the project doesn't exist.
    """
    wanted = sections & _PROJECT_SECTIONS
    if not wanted:
        # Validate the project still exists so a typo / wrong cwd fails
        # fast instead of returning an empty status silently.
        discovery.require_project_db(paths, project)
        return ProjectStatus(worktrees=[], prs=[], stacker=[])
    wt_rows = {w: (r, u) for w, r, u in discovery.read_wts(paths, project)}
    findings = check_mod.check_project(paths, project)
    repo_branches: dict[str, dict[Path, str]] = {}
    ctx = StackerCtx.default(paths)
    pr_map: dict[tuple[str, str], PRState] = {
        (pr.repo_name, pr.branch): pr for pr in ctx.db.list_pr_states()
    }
    # Bulk-load every tracked branch so the per-row `tracked` check and
    # the stacker-arm gatherer both consult the same in-memory view.
    tracked_by_key: dict[tuple[str, str], TrackedBranch] = {
        (tb.repo_name, tb.branch): tb for tb in ctx.db.list_branches()
    }
    want_worktrees = StatusSection.WORKTREES in sections
    want_prs = StatusSection.PRS in sections
    want_stacker = StatusSection.STACKER in sections
    worktrees: list[WorktreeRow] = []
    prs: list[PRRow] = []
    tracked_per_repo: dict[str, list[TrackedBranch]] = {}
    wt_labels: dict[tuple[str, str], str] = {}
    for f in findings:
        repo = f.repo
        if f.wt is not None and f.wt in wt_rows:
            row_repo, _ = wt_rows[f.wt]
            repo = repo or row_repo
        branch = _resolve_branch(f, repo_branches, paths, project)
        pr = pr_map.get((repo, branch)) if repo and branch else None
        tracked_entry = tracked_by_key.get((repo, branch)) if repo and branch else None
        if want_worktrees:
            worktrees.append(
                WorktreeRow(
                    wt=f.wt,
                    repo=repo,
                    branch=branch,
                    pr=pr,
                    tracked=tracked_entry is not None,
                    finding=f,
                ),
            )
        if (
            want_prs
            and pr is not None
            and f.wt is not None
            and repo is not None
            and branch is not None
        ):
            prs.append(PRRow(wt=f.wt, repo=repo, branch=branch, pr=pr))
        if want_stacker and tracked_entry is not None and repo is not None:
            tracked_per_repo.setdefault(repo, []).append(tracked_entry)
        # Every live worktree with a known branch contributes a label,
        # even if untracked — so if a branch later becomes tracked but
        # the renderer consults this map, it still gets the right label.
        if want_stacker and f.wt is not None and repo is not None and branch is not None:
            wt_labels[(repo, branch)] = f.wt
    prs.sort(key=lambda r: (r.repo, r.wt))
    stacker = (
        _gather_stacker(ctx, paths, tracked_per_repo, wt_labels, structured=stacker_structured)
        if want_stacker
        else []
    )
    return ProjectStatus(worktrees=worktrees, prs=prs, stacker=stacker)
