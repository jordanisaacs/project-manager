"""Parser shape + dispatch tests for the `pm stacker` CLI (cyclopts-based).

These exercise parsing — no service calls — so assertions target the aligned
command surface: the subcommand set, scope flag grammar, and the handlers
wired to each command.
"""
import pytest

# Import for side-effect: registers every sub-app/command on `root`.
import project_manager.cli  # noqa: F401
from project_manager.cli._shared import root
from project_manager.stacker.commands import (
    abort as abort_cmd,
)
from project_manager.stacker.commands import (
    config as config_cmd,
)
from project_manager.stacker.commands import (
    continue_ as continue_cmd,
)
from project_manager.stacker.commands import (
    create as create_cmd,
)
from project_manager.stacker.commands import (
    guard as guard_cmd,
)
from project_manager.stacker.commands import (
    log as log_cmd,
)
from project_manager.stacker.commands import (
    ls as ls_cmd,
)
from project_manager.stacker.commands import (
    push as push_cmd,
)
from project_manager.stacker.commands import (
    remove as remove_cmd,
)
from project_manager.stacker.commands import (
    rename as rename_cmd,
)
from project_manager.stacker.commands import (
    repair as repair_cmd,
)
from project_manager.stacker.commands import (
    reparent as reparent_cmd,
)
from project_manager.stacker.commands import (
    split as split_cmd,
)
from project_manager.stacker.commands import (
    sync as sync_cmd,
)


def _parse(*argv: str):
    """Return (command_fn, bound_args) for `pm stacker <argv>`."""
    command, bound, _ = root.parse_args(["stacker", *argv])
    return command, bound


def test_removed_commands_no_longer_exist() -> None:
    """Hard rename: `pr`, `graph`, `status`, `track`, `untrack` stay gone."""
    for removed in ("pr", "graph", "status", "track", "untrack"):
        with pytest.raises(SystemExit):
            _parse(removed, "main")


@pytest.mark.parametrize(
    ("subcommand", "args", "expected_fn"),
    [
        ("create", ["feature-a"], create_cmd.create),
        ("sync", [], sync_cmd.sync),
        ("push", [], push_cmd.push),
        ("ls", [], ls_cmd.ls),
        ("remove", [], remove_cmd.remove),
        ("reparent", ["main"], reparent_cmd.reparent),
        ("split", ["new", "abc"], split_cmd.split),
        ("rename", ["b2"], rename_cmd.rename),
        ("repair", ["main"], repair_cmd.repair),
        ("log", [], log_cmd.log),
        ("continue", [], continue_cmd.continue_),
        ("abort", [], abort_cmd.abort),
        ("config", ["pr.mode"], config_cmd.config),
        ("guard", ["no-rebase"], guard_cmd.no_rebase),
    ],
)
def test_every_subcommand_is_registered(
    subcommand: str, args: list[str], expected_fn,
) -> None:
    fn, _ = _parse(subcommand, *args)
    assert fn is expected_fn


def test_sync_accepts_scope_flags() -> None:
    _, bound = _parse("sync", "-a", "--skip-descendants", "--from", "feature-x")
    scope = bound.arguments["scope"]
    assert scope.all is True
    assert scope.skip_descendants is True
    assert scope.from_branch == "feature-x"


def test_sync_continue_abort_flags_exist() -> None:
    _, cont = _parse("sync", "--continue")
    _, abrt = _parse("sync", "--abort")
    assert cont.arguments["continue_"] is True
    assert abrt.arguments["abort"] is True


def test_push_only_flag_available() -> None:
    _, bound = _parse("push", "--only")
    assert bound.arguments["only"] is True


def test_push_create_pr_negates() -> None:
    _, bound = _parse("push", "--no-create-pr")
    assert bound.arguments["create_pr"] is False


def test_ls_details_choices_enforced() -> None:
    with pytest.raises((SystemExit, Exception)):
        _parse("ls", "--details", "bogus")
    _, bound = _parse("ls", "--details", "status-counts")
    assert bound.arguments["details"] == "status-counts"


def test_create_replace_and_copy_flags() -> None:
    _, a = _parse("create", "feature", "--replace", "--on", "main")
    assert a.arguments["replace"] is True
    assert a.arguments["on"] == "main"

    _, b = _parse("create", "feature", "--copy", "other", "--on", "current")
    assert b.arguments["copy"] == "other"


def test_remove_has_keep_branch_flag() -> None:
    _, bound = _parse("remove", "feature-a", "--keep-branch")
    assert bound.arguments["keep_branch"] is True
    assert bound.arguments["branch"] == "feature-a"


def test_reparent_accepts_new_parent_positional() -> None:
    _, bound = _parse("reparent", "main", "--branch", "feature-a")
    assert bound.arguments["new_parent"] == "main"
    assert bound.arguments["branch"] == "feature-a"


def test_reparent_continue_abort_flags() -> None:
    _, cont = _parse("reparent", "--continue")
    _, abrt = _parse("reparent", "--abort")
    assert cont.arguments["continue_"] is True
    assert abrt.arguments["abort"] is True


def test_split_requires_new_name_and_commit() -> None:
    with pytest.raises((SystemExit, Exception)):
        _parse("split", "new-name")  # missing commit
    _, bound = _parse("split", "new-name", "abc123", "--stay")
    assert bound.arguments["new_name"] == "new-name"
    assert bound.arguments["commit"] == "abc123"
    assert bound.arguments["stay"] is True


def test_rename_requires_new_name() -> None:
    with pytest.raises((SystemExit, Exception)):
        _parse("rename")  # missing new_name
    _, bound = _parse("rename", "b2")
    assert bound.arguments["new_name"] == "b2"
