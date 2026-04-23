"""Parser shape + dispatch tests for the `pm stacker` CLI.

These exercise argparse directly — no service calls — so assertions
target the aligned command surface: the new subcommand set, scope flag
grammar, and the handlers wired to each command.
"""
from __future__ import annotations

import argparse

import pytest

from project_manager.stacker import cli as stacker_cli


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pm")
    subs = parser.add_subparsers(dest="group", required=True)
    stacker_cli.add_subparser(subs)
    return parser


def _parse(*argv: str) -> argparse.Namespace:
    return _parser().parse_args(["stacker", *argv])


def test_removed_commands_no_longer_exist() -> None:
    """Hard rename: `pr`, `graph`, `status`, `track`, `untrack`, old `push` are gone."""
    for removed in ("pr", "graph", "status", "track", "untrack"):
        with pytest.raises(SystemExit):
            _parse(removed, "main")


@pytest.mark.parametrize(
    ("subcommand", "args"),
    [
        ("create", ["feature-a"]),
        ("sync", []),
        ("push", []),
        ("ls", []),
        ("remove", []),
        ("reparent", ["main"]),
        ("split", ["new", "abc"]),
        ("rename", ["b2"]),
        ("log", []),
        ("continue", []),
        ("abort", []),
        ("config", ["pr.mode"]),
        ("guard", ["no-rebase"]),
    ],
)
def test_every_new_subcommand_is_registered(
    subcommand: str, args: list[str],
) -> None:
    # parse_args returning without SystemExit proves the subcommand is
    # registered and its required args are satisfied by the stub values.
    parsed = _parse(subcommand, *args)
    assert parsed.cmd == subcommand or parsed.cmd is None


def test_sync_accepts_scope_flags() -> None:
    args = _parse("sync", "-a", "--skip-descendants", "--from", "feature-x")
    assert args.all is True
    assert args.skip_descendants is True
    assert args.from_branch == "feature-x"


def test_sync_continue_abort_flags_exist() -> None:
    args_cont = _parse("sync", "--continue")
    args_abort = _parse("sync", "--abort")
    assert args_cont.continue_ is True
    assert args_abort.abort is True


def test_push_draft_publish_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        _parse("push", "--draft", "--publish")


def test_push_only_flag_available() -> None:
    args = _parse("push", "--only")
    assert args.only is True


def test_push_create_pr_parses_bool() -> None:
    args = _parse("push", "--create-pr", "false")
    assert args.create_pr is False


def test_ls_details_choices_enforced() -> None:
    with pytest.raises(SystemExit):
        _parse("ls", "--details", "bogus")
    args = _parse("ls", "--details", "status-counts")
    assert args.details == "status-counts"


def test_create_replace_and_copy_flags() -> None:
    args = _parse("create", "feature", "--replace", "--on", "main")
    assert args.replace is True
    assert args.on == "main"

    args2 = _parse("create", "feature", "--copy", "other", "--on", "current")
    assert args2.copy == "other"


def test_remove_has_keep_branch_flag() -> None:
    args = _parse("remove", "--keep-branch", "feature-a")
    assert args.keep_branch is True
    assert args.branch == "feature-a"


def test_reparent_accepts_new_parent_positional() -> None:
    args = _parse("reparent", "main", "--branch", "feature-a")
    assert args.new_parent == "main"
    assert args.branch == "feature-a"


def test_reparent_continue_abort_flags() -> None:
    args_cont = _parse("reparent", "--continue")
    args_abort = _parse("reparent", "--abort")
    assert args_cont.continue_ is True
    assert args_abort.abort is True


def test_split_requires_new_name_and_commit() -> None:
    with pytest.raises(SystemExit):
        _parse("split", "new-name")  # missing commit
    args = _parse("split", "new-name", "abc123", "--stay")
    assert args.new_name == "new-name"
    assert args.commit == "abc123"
    assert args.stay is True


def test_rename_requires_new_name() -> None:
    with pytest.raises(SystemExit):
        _parse("rename")  # missing new_name
    args = _parse("rename", "b2")
    assert args.new_name == "b2"


def test_guard_no_rebase_subcommand_wired() -> None:
    args = _parse("guard", "no-rebase")
    assert args.guard_cmd == "no-rebase"
    assert args.func is stacker_cli._cmd_guard_no_rebase


def test_config_mutually_exclusive_list_unset() -> None:
    with pytest.raises(SystemExit):
        _parse("config", "--list", "--unset", "pr.mode")
