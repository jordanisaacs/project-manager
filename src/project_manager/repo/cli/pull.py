"""`pm repo pull`."""
from project_manager import config
from project_manager.repo import pull as pull_mod

from . import repo_app


@repo_app.command
def pull(*, repo: str | None = None) -> int:
    """Fetch + ff-only pull each canonical repo.

    --repo accepts a comma-separated list; defaults to every repo.
    """
    paths = config.load()
    repos = [r.strip() for r in repo.split(",") if r.strip()] if repo else None
    results = pull_mod.pull(paths, repos)
    any_fail = False
    for r in results:
        print(f"{r.repo}\t{'ok' if r.ok else 'fail'}\t{r.message}")
        if not r.ok:
            any_fail = True
    return 1 if any_fail else 0
