from __future__ import annotations

import argparse
import sys

from project_manager import config as config_mod
from project_manager.stacker import git
from project_manager.stacker.service import StackerService

from . import _common


def add(sub: argparse._SubParsersAction) -> None:
    cfg = sub.add_parser(
        "config", help="get/set per-repo stacker config (git-config style)"
    )
    cfg.add_argument("--repo", default=None, help="defaults to the cwd's pm slot")
    group = cfg.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="list all set keys")
    group.add_argument("--unset", action="store_true", help="remove a key")
    cfg.add_argument("key", nargs="?", help="config key (e.g. pr.mode)")
    cfg.add_argument("value", nargs="?", help="value to set; omit to read")
    cfg.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    paths = config_mod.load()
    service = _common.service(paths)
    try:
        repo_name = _common.resolve_repo(args, paths)
        return _dispatch(service, args, repo_name)
    except git.GitError as e:
        print(f"pm: {e}", file=sys.stderr)
        return 2


def _dispatch(
    service: StackerService, args: argparse.Namespace, repo_name: str
) -> int:
    if args.list:
        return _list(service, args, repo_name)
    if args.unset:
        return _unset(service, args, repo_name)
    if args.key is None:
        print("pm: config requires a key (or --list / --unset).", file=sys.stderr)
        return 2
    if args.value is None:
        return _get(service, args, repo_name)
    return _set(service, args, repo_name)


def _list(
    service: StackerService, args: argparse.Namespace, repo_name: str
) -> int:
    if args.key is not None or args.value is not None:
        print("pm: --list takes no key/value arguments.", file=sys.stderr)
        return 2
    for key, value in service.list_config(repo_name):
        print(f"{key}={value}")
    return 0


def _unset(
    service: StackerService, args: argparse.Namespace, repo_name: str
) -> int:
    if args.key is None or args.value is not None:
        print("pm: --unset requires exactly one key.", file=sys.stderr)
        return 2
    return 0 if service.unset_config(repo_name, args.key) else 1


def _get(
    service: StackerService, args: argparse.Namespace, repo_name: str
) -> int:
    current = service.get_config(repo_name, args.key)
    if current is None:
        return 1
    print(current)
    return 0


def _set(
    service: StackerService, args: argparse.Namespace, repo_name: str
) -> int:
    for note in service.set_config(repo_name, args.key, args.value):
        print(note, file=sys.stderr)
    return 0
