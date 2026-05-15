"""`pm check` — top-level invariant checker."""

import sys
from typing import Annotated

from cyclopts import Parameter

from project_manager import check as check_mod
from project_manager import config, render

from ._shared import root


@root.command
def check(
    *,
    fix: bool = False,
    json: Annotated[bool, Parameter(negative="")] = False,
) -> int:
    """Verify invariants across pool and projects.

    Exits 1 when any non-healthy finding remains (and --fix was not passed).
    """
    paths = config.load()
    findings = check_mod.check(paths)
    render.emit_rows(findings, check_mod.COLUMNS, as_json=json)
    if fix:
        applied = check_mod.fix(paths, findings)
        print(f"# applied {applied} fix(es)", file=sys.stderr)
    non_healthy = [f for f in findings if f.kind != check_mod.Kind.ACTIVE]
    return 1 if non_healthy and not fix else 0
