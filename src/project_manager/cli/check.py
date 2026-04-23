"""`pm check` — top-level invariant checker."""
import sys

from project_manager import check as check_mod
from project_manager import config

from ._shared import root


@root.command
def check(*, fix: bool = False) -> int:
    """Verify invariants across pool and projects.

    Exits 1 when any non-healthy finding remains (and --fix was not passed).
    """
    paths = config.load()
    findings = check_mod.check(paths)
    for f in findings:
        slot = f.slot_path if f.slot_path is not None else "-"
        forward = f.forward_path if f.forward_path is not None else "-"
        print(f"{f.kind.value}\t{forward}\t{slot}\t{f.detail}")
    if fix:
        applied = check_mod.fix(paths, findings)
        print(f"# applied {applied} fix(es)", file=sys.stderr)
    non_healthy = [f for f in findings if f.kind != check_mod.Kind.ACTIVE]
    return 1 if non_healthy and not fix else 0
