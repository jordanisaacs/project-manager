import argparse
import sys

from project_manager import check as check_mod
from project_manager import config
from project_manager.errors import ProjectError
from project_manager.project import attach as attach_mod
from project_manager.project import current
from project_manager.project import delete as delete_mod
from project_manager.project import detach as detach_mod
from project_manager.project import ls as ls_mod
from project_manager.project import new as new_mod
from project_manager.project import status as status_mod


def _parse_repos(value: str) -> list[str]:
    return [r.strip() for r in value.split(",") if r.strip()]


def _selected_repos(args: argparse.Namespace) -> list[str] | None:
    """Translate --repos / --all into the list[str] | None convention used by impls."""
    if getattr(args, "all", False):
        return None
    return _parse_repos(args.repos)


def _cmd_new(args: argparse.Namespace) -> int:
    paths = config.load()
    try:
        claimed = new_mod.new(paths, args.project, _parse_repos(args.repos))
    except ProjectError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    for repo, slot in claimed:
        print(f"{repo}\t{slot.path}")
    return 0


def _cmd_attach(args: argparse.Namespace) -> int:
    paths = config.load()
    try:
        project = current.resolve_project(paths, args.project)
        attached = attach_mod.attach(paths, project, _selected_repos(args))
    except ProjectError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    for slot in attached:
        print(f"{slot.repo}\t{slot.path}")
    return 0


def _cmd_detach(args: argparse.Namespace) -> int:
    paths = config.load()
    try:
        project = current.resolve_project(paths, args.project)
        released = detach_mod.detach(paths, project, _selected_repos(args))
    except ProjectError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    for slot in released:
        print(f"{slot.repo}\t{slot.path}")
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    paths = config.load()
    repos = _parse_repos(args.repos) if args.repos else None
    try:
        project = current.resolve_project(paths, args.project)
        delete_mod.delete(paths, project, repos)
    except ProjectError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return 0


def _cmd_ls(_: argparse.Namespace) -> int:
    paths = config.load()
    rows = ls_mod.ls(paths)
    for row in rows:
        print(f"{row.project}\t{row.repo}\t{row.slot_uuid}\t{row.status}")
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
        repo = row.repo if row.repo is not None else "-"
        uuid = row.slot_uuid if row.slot_uuid is not None else "-"
        forward = f.forward_path if f.forward_path is not None else "-"
        slot = f.slot_path if f.slot_path is not None else "-"
        print(f"{repo}\t{uuid}\t{f.kind.value}\t{forward}\t{slot}\t{f.detail}")
    non_healthy = [r for r in rows if r.finding.kind != check_mod.Kind.ACTIVE]
    return 1 if non_healthy else 0


def _add_repos_or_all(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--repos", help="comma-separated repo names")
    group.add_argument("--all", action="store_true", help="every repo in the project")


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    project = subparsers.add_parser("project", help="manage projects")
    sub = project.add_subparsers(dest="cmd", required=True)

    new = sub.add_parser("new", help="create a project and claim a slot per repo")
    new.add_argument("project", metavar="<project>")
    new.add_argument("--repos", required=True, help="comma-separated repo names")
    new.set_defaults(func=_cmd_new)

    attach = sub.add_parser("attach", help="re-attach a project (best-effort slot reclaim)")
    attach.add_argument("project", nargs="?", metavar="<project>")
    _add_repos_or_all(attach)
    attach.set_defaults(func=_cmd_attach)

    detach = sub.add_parser("detach", help="detach: unlink forward + release .owner")
    detach.add_argument("project", nargs="?", metavar="<project>")
    _add_repos_or_all(detach)
    detach.set_defaults(func=_cmd_detach)

    delete = sub.add_parser(
        "delete", help="delete project (or --repos r1,r2 for per-repo delete)",
    )
    delete.add_argument("project", nargs="?", metavar="<project>")
    delete.add_argument(
        "--repos", default=None, help="comma-separated repos to delete (omit for whole project)",
    )
    delete.set_defaults(func=_cmd_delete)

    ls = sub.add_parser("ls", help="list projects")
    ls.set_defaults(func=_cmd_ls)

    status = sub.add_parser("status", help="show health + db rows for a single project")
    status.add_argument("project", nargs="?", metavar="<project>")
    status.set_defaults(func=_cmd_status)
