"""`pm stacker config` — get/set per-repo stacker config (git-config style)."""
import sys
from typing import Annotated

from cyclopts import Parameter

from project_manager import config as config_mod
from project_manager.cli._shared import RepoFlag

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
        for k, v in svc.list_config(repo_name):
            print(f"{k}={v}")
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
        print(current)
        return 0
    for note in svc.set_config(repo_name, key, value):
        print(note, file=sys.stderr)
    return 0
