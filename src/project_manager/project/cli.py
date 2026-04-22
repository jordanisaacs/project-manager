import argparse
import sys

from project_manager import config
from project_manager.project import delete as delete_mod
from project_manager.project import ls as ls_mod
from project_manager.project import new as new_mod
from project_manager.project import release as release_mod
from project_manager.project.errors import ProjectError


def _cmd_new(args: argparse.Namespace) -> int:
    paths = config.load()
    repos = [r.strip() for r in args.repos.split(",") if r.strip()]
    try:
        claimed = new_mod.new(paths, args.name, repos)
    except ProjectError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    for repo, slot in claimed:
        print(f"{repo}\t{slot.path}")
    return 0


def _cmd_release(args: argparse.Namespace) -> int:
    paths = config.load()
    try:
        released = release_mod.release(paths, args.name)
    except ProjectError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    for slot in released:
        print(f"{slot.repo}\t{slot.path}")
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    paths = config.load()
    try:
        delete_mod.delete(paths, args.name)
    except ProjectError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2
    return 0


def _cmd_ls(_: argparse.Namespace) -> int:
    paths = config.load()
    rows = ls_mod.ls(paths)
    for row in rows:
        print(f"{row.project}\t{row.repo}\t{row.uuid}\t{row.status}")
    return 0


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    project = subparsers.add_parser("project", help="manage projects")
    sub = project.add_subparsers(dest="cmd", required=True)

    new = sub.add_parser("new", help="claim a slot per repo for a new project")
    new.add_argument("name")
    new.add_argument("--repos", required=True, help="comma-separated repo names")
    new.set_defaults(func=_cmd_new)

    release = sub.add_parser("release", help="release ownership (keep forward symlinks)")
    release.add_argument("name")
    release.set_defaults(func=_cmd_release)

    delete = sub.add_parser("delete", help="release + remove forward symlinks + rmdir")
    delete.add_argument("name")
    delete.set_defaults(func=_cmd_delete)

    ls = sub.add_parser("ls", help="list projects")
    ls.set_defaults(func=_cmd_ls)
