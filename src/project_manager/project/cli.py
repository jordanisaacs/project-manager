import argparse
import sys

from project_manager import check as check_mod
from project_manager import config
from project_manager.errors import ProjectError
from project_manager.paths import Paths
from project_manager.project import attach as attach_mod
from project_manager.project import create as create_mod
from project_manager.project import current
from project_manager.project import delete as delete_mod
from project_manager.project import detach as detach_mod
from project_manager.project import ls as ls_mod
from project_manager.project import status as status_mod


def _parse_wts(value: str) -> list[str]:
    return [w.strip() for w in value.split(",") if w.strip()]


def _parse_wt_spec(value: str) -> list[tuple[str, str]]:
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
        parts = item.split(":")
        if len(parts) == 1:
            wt = repo = parts[0].strip()
        elif len(parts) == 2:  # noqa: PLR2004
            wt, repo = parts[0].strip(), parts[1].strip()
        else:
            raise ProjectError(f"worktree entry has too many colons: {item!r}")
        if not wt or not repo:
            raise ProjectError(f"worktree entry has empty name or repo: {item!r}")
        if wt in seen:
            raise ProjectError(f"duplicate worktree name '{wt}' in spec")
        seen.add(wt)
        out.append((wt, repo))
    return out


def _selected_wts(args: argparse.Namespace) -> list[str] | None:
    """Translate --wt / --all into the list[str] | None convention used by impls."""
    if getattr(args, "all", False):
        return None
    return _parse_wts(args.wt)


# --- top-level project commands ---


def _cmd_create(args: argparse.Namespace) -> int:
    paths = config.load()
    try:
        spec = _parse_wt_spec(args.wt) if args.wt else []
        claimed = create_mod.create(paths, args.project, spec)
    except ProjectError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    for wt, slot in claimed:
        print(f"{wt}\t{slot.path}")
    return 0


def _cmd_project_delete(args: argparse.Namespace) -> int:
    paths = config.load()
    try:
        if args.dry_run:
            plan = delete_mod.plan_delete(paths, args.project, None)
            _print_delete_plan(plan, paths)
            return 1 if plan.has_blocker else 0
        delete_mod.delete(paths, args.project, None)
    except ProjectError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return 0


def _cmd_ls(_: argparse.Namespace) -> int:
    paths = config.load()
    rows = ls_mod.ls(paths)
    for row in rows:
        print(f"{row.project}\t{row.wt}\t{row.repo}\t{row.slot_uuid}\t{row.status}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    paths = config.load()
    try:
        project = current.resolve_project(paths, args.project)
        rows = status_mod.status(paths, project)
    except ProjectError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    for row in rows:
        f = row.finding
        wt = row.wt if row.wt is not None else "-"
        repo = row.repo if row.repo is not None else "-"
        uuid = row.slot_uuid if row.slot_uuid is not None else "-"
        forward = f.forward_path if f.forward_path is not None else "-"
        slot = f.slot_path if f.slot_path is not None else "-"
        print(
            f"{wt}\t{repo}\t{uuid}\t{f.kind.value}\t{forward}\t{slot}\t{f.detail}"
        )
    non_healthy = [r for r in rows if r.finding.kind != check_mod.Kind.ACTIVE]
    return 1 if non_healthy else 0


# --- wt subcommands ---


def _cmd_wt_create(args: argparse.Namespace) -> int:
    paths = config.load()
    try:
        project = current.resolve_project(paths, args.project)
        spec = _parse_wt_spec(args.spec)
        claimed = create_mod.create(paths, project, spec)
    except ProjectError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    for wt, slot in claimed:
        print(f"{wt}\t{slot.path}")
    return 0


def _cmd_wt_attach(args: argparse.Namespace) -> int:
    paths = config.load()
    try:
        project = current.resolve_project(paths, args.project)
        result = attach_mod.attach(
            paths, project, _selected_wts(args), no_branch=args.no_branch,
        )
    except ProjectError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    for warning in result.warnings:
        print(f"pm: warn: {warning}", file=sys.stderr)
    for attached in result.newly_claimed:
        print(f"{attached.wt}\t{attached.path}")
    return 0


def _cmd_wt_detach(args: argparse.Namespace) -> int:
    paths = config.load()
    selected = _selected_wts(args)
    try:
        project = current.resolve_project(paths, args.project)
        if args.dry_run:
            plan = detach_mod.plan_detach(paths, project, selected)
            _print_detach_plan(plan)
            return 1 if plan.has_blocker else 0
        released = detach_mod.detach(paths, project, selected)
    except ProjectError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    for d in released:
        print(f"{d.wt}\t{d.path}")
    return 0


def _cmd_wt_delete(args: argparse.Namespace) -> int:
    paths = config.load()
    selected = _selected_wts(args)
    try:
        project = current.resolve_project(paths, args.project)
        if args.dry_run:
            plan = delete_mod.plan_delete(paths, project, selected)
            _print_delete_plan(plan, paths)
            return 1 if plan.has_blocker else 0
        delete_mod.delete(paths, project, selected)
    except ProjectError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return 0


# --- plan printers ---


def _print_detach_plan(plan: detach_mod.DetachPlan) -> None:
    for action in plan.actions:
        if action.blocker is not None:
            print(
                f"dry-run: would detach {action.wt}\tBLOCKED: {action.blocker}",
                file=sys.stderr,
            )
            continue
        if action.kind == "noop":
            print(f"dry-run: {action.wt} already detached")
            continue
        print(f"dry-run: would detach {action.wt}\trelease slot {action.slot_uuid}")


def _print_delete_plan(plan: delete_mod.DeletePlan, paths: Paths) -> None:
    for extra in plan.extras:
        print(f"dry-run: BLOCKED: non-pm entry {extra}", file=sys.stderr)
    _print_detach_plan(plan.detach_plan)
    for wt in plan.drop_rows:
        print(f"dry-run: would drop db row {wt}")
    if plan.remove_readme:
        print(f"dry-run: would remove {paths.project(plan.project) / 'README.md'}")
    if plan.drop_db:
        print(f"dry-run: would drop {paths.project_db(plan.project)}")
    if plan.rmdir:
        print(f"dry-run: would rmdir {paths.project(plan.project)}")


# --- subparser wiring ---


def _add_wts_or_all(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--wt", help="comma-separated worktree names")
    group.add_argument("--all", action="store_true", help="every worktree in the project")


def _add_project_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-p", "--project", default=None, metavar="<project>",
        help="project name (defaults to current project)",
    )


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    project = subparsers.add_parser("project", help="manage projects")
    sub = project.add_subparsers(dest="cmd", required=True)

    create = sub.add_parser(
        "create", help="create a project (optionally with initial worktrees)"
    )
    create.add_argument("project", metavar="<project>")
    create.add_argument(
        "--wt",
        default=None,
        help="comma-separated worktrees: '<repo>' or '<name>:<repo>'",
    )
    create.set_defaults(func=_cmd_create)

    delete = sub.add_parser("delete", help="delete a whole project")
    delete.add_argument("project", metavar="<project>")
    delete.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without mutating state; exit 1 if any blocker",
    )
    delete.set_defaults(func=_cmd_project_delete)

    ls = sub.add_parser("ls", help="list projects")
    ls.set_defaults(func=_cmd_ls)

    status = sub.add_parser("status", help="show health + db rows for a single project")
    status.add_argument(
        "project", nargs="?", default=None, metavar="<project>",
        help="project name (defaults to current project)",
    )
    status.set_defaults(func=_cmd_status)

    wt = sub.add_parser("wt", help="manage worktrees within a project")
    wt_sub = wt.add_subparsers(dest="wt_cmd", required=True)

    wt_create = wt_sub.add_parser(
        "create", help="add worktree(s) to an existing project"
    )
    _add_project_flag(wt_create)
    wt_create.add_argument(
        "spec",
        metavar="<spec>",
        help="comma-separated worktrees: '<repo>' or '<name>:<repo>'",
    )
    wt_create.set_defaults(func=_cmd_wt_create)

    wt_attach = wt_sub.add_parser(
        "attach", help="re-attach worktrees (best-effort slot reclaim)"
    )
    _add_project_flag(wt_attach)
    wt_attach.add_argument(
        "--no-branch",
        action="store_true",
        help="skip saved-branch restore (leave slot in detached HEAD)",
    )
    _add_wts_or_all(wt_attach)
    wt_attach.set_defaults(func=_cmd_wt_attach)

    wt_detach = wt_sub.add_parser(
        "detach", help="detach worktrees: unlink forward + release pool row"
    )
    _add_project_flag(wt_detach)
    wt_detach.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without mutating state; exit 1 if any blocker",
    )
    _add_wts_or_all(wt_detach)
    wt_detach.set_defaults(func=_cmd_wt_detach)

    wt_delete = wt_sub.add_parser("delete", help="delete worktree row(s) from a project")
    _add_project_flag(wt_delete)
    wt_delete.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan without mutating state; exit 1 if any blocker",
    )
    _add_wts_or_all(wt_delete)
    wt_delete.set_defaults(func=_cmd_wt_delete)


