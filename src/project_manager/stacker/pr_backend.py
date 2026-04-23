from __future__ import annotations

from pathlib import Path
from typing import Protocol

from . import gh


class PRBackend(Protocol):
    """Injected seam over `stacker.gh` so PR-creation paths are testable.

    Production impl (`GhCliBackend`) shells out to `gh`; tests swap in a
    recording fake.
    """

    def repo_info(
        self, *, cwd: Path | None = None, repo: str | None = None
    ) -> gh.RepoInfo: ...

    def list_open_prs(
        self,
        repo: str,
        *,
        head: str | None = None,
        search: str | None = None,
    ) -> list[gh.PullRequest]: ...

    def create_pr(self, request: gh.CreatePRRequest) -> str: ...

    def edit_pr(self, request: gh.EditPRRequest) -> None: ...


class GhCliBackend:
    """Production backend — delegates to `stacker.gh` module-level functions."""

    def repo_info(
        self, *, cwd: Path | None = None, repo: str | None = None
    ) -> gh.RepoInfo:
        return gh.repo_info(cwd=cwd, repo=repo)

    def list_open_prs(
        self,
        repo: str,
        *,
        head: str | None = None,
        search: str | None = None,
    ) -> list[gh.PullRequest]:
        return gh.list_open_prs(repo, head=head, search=search)

    def create_pr(self, request: gh.CreatePRRequest) -> str:
        return gh.create_pr(request)

    def edit_pr(self, request: gh.EditPRRequest) -> None:
        gh.edit_pr(request)
