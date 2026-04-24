"""`pm stacker config` — get/set per-repo stacker config (git-config style)."""
from typing import Annotated

from cyclopts import Parameter

from project_manager import config as config_mod
from project_manager import render
from project_manager.cli._shared import RepoFlag
from project_manager.stacker.pr.config import CONFIG_COLUMNS, ConfigRow

from . import _common, stacker_app


@stacker_app.command
def config(
    key: str | None = None,
    value: str | None = None,
    *,
    flag: RepoFlag = RepoFlag(),
    list_: Annotated[
        bool,
        Parameter(name="--list", negative="", help="list all set keys"),
    ] = False,
    unset: Annotated[
        bool,
        Parameter(negative="", help="remove a key"),
    ] = False,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Get/set per-repo stacker config.

    Without arguments: `config --list`. With a single key: read. With key + value:
    set. `--unset <key>` removes.
    """
    if list_ and unset:
        raise ValueError("--list and --unset are mutually exclusive.")
    paths = config_mod.load()
    svc = _common.service(paths)
    repo_name = _common.resolve_repo(flag.repo, paths)
    if list_:
        if key is not None or value is not None:
            raise ValueError("--list takes no key/value arguments.")
        rows = [ConfigRow(key=k, value=v) for k, v in svc.list_config(repo_name)]
        render.emit_rows(rows, CONFIG_COLUMNS, as_json=json)
        return 0
    if unset:
        if key is None or value is not None:
            raise ValueError("--unset requires exactly one key.")
        return 0 if svc.unset_config(repo_name, key) else 1
    if key is None:
        raise ValueError("config requires a key (or --list / --unset).")
    if value is None:
        current = svc.get_config(repo_name, key)
        if current is None:
            return 1
        render.console().print(current, markup=False, highlight=False)
        return 0
    err = render.console(stderr=True)
    for note in svc.set_config(repo_name, key, value):
        err.print(note, markup=False, highlight=False)
    return 0
