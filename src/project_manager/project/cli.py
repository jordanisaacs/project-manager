import argparse
import sys

from project_manager import config
from project_manager.project import attach as attach_mod
from project_manager.project import delete as delete_mod
from project_manager.project import detach as detach_mod
from project_manager.project import ls as ls_mod
from project_manager.project import new as new_mod
from project_manager.project.errors import ProjectError


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
        claimed = new_mod.new(paths, args.name, _parse_repos(args.repos))
    except ProjectError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    for repo, slot in claimed:
        print(f"{repo}\t{slot.path}")
    return 0


def _cmd_attach(args: argparse.Namespace) -> int:
    paths = config.load()
    try:
        attached = attach_mod.attach(paths, args.name, _selected_repos(args))
    except ProjectError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    for slot in attached:
        print(f"{slot.repo}\t{slot.path}")
    return 0


def _cmd_detach(args: argparse.Namespace) -> int:
    paths = config.load()
    try:
        released = detach_mod.detach(paths, args.name, _selected_repos(args))
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
        delete_mod.delete(paths, args.name, repos)
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


def _add_repos_or_all(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--repos", help="comma-separated repo names")
    group.add_argument("--all", action="store_true", help="every repo in the project")


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    project = subparsers.add_parser("project", help="manage projects")
    sub = project.add_subparsers(dest="cmd", required=True)

    new = sub.add_parser("new", help="create a project and claim a slot per repo")
    new.add_argument("name")
    new.add_argument("--repos", required=True, help="comma-separated repo names")
    new.set_defaults(func=_cmd_new)

    attach = sub.add_parser("attach", help="re-attach a project (best-effort slot reclaim)")
    attach.add_argument("name")
    _add_repos_or_all(attach)
    attach.set_defaults(func=_cmd_attach)

    detach = sub.add_parser("detach", help="detach: unlink forward + release .owner")
    detach.add_argument("name")
    _add_repos_or_all(detach)
    detach.set_defaults(func=_cmd_detach)

    delete = sub.add_parser(
        "delete", help="delete project (or --repos r1,r2 for per-repo delete)"
    )
    delete.add_argument("name")
    delete.add_argument(
        "--repos", default=None, help="comma-separated repos to delete (omit for whole project)"
    )
    delete.set_defaults(func=_cmd_delete)

    ls = sub.add_parser("ls", help="list projects")
    ls.set_defaults(func=_cmd_ls)
