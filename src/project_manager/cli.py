import argparse
import sys

from project_manager import check as check_mod
from project_manager import config
from project_manager.pool import cli as pool_cli
from project_manager.project import cli as project_cli
from project_manager.repo import cli as repo_cli
from project_manager.stacker import cli as stacker_cli


def _cmd_check(args: argparse.Namespace) -> int:
    paths = config.load()
    findings = check_mod.check(paths)
    for f in findings:
        slot = f.slot_path if f.slot_path is not None else "-"
        forward = f.forward_path if f.forward_path is not None else "-"
        print(f"{f.kind.value}\t{forward}\t{slot}\t{f.detail}")
    if args.fix:
        applied = check_mod.fix(paths, findings)
        print(f"# applied {applied} fix(es)", file=sys.stderr)
    non_healthy = [f for f in findings if f.kind != check_mod.Kind.ACTIVE]
    return 1 if non_healthy and not args.fix else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pm",
        description="Project manager: pool of git worktrees bound to named projects via symlinks.",
    )
    subparsers = parser.add_subparsers(dest="group", required=True)

    project_cli.add_subparser(subparsers)
    pool_cli.add_subparser(subparsers)
    repo_cli.add_subparser(subparsers)
    stacker_cli.add_subparser(subparsers)

    check = subparsers.add_parser("check", help="verify invariants across pool and projects")
    check.add_argument("--fix", action="store_true", help="apply safe reconciliations")
    check.set_defaults(func=_cmd_check)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
