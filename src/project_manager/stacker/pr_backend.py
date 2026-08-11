from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import gh


class PRBackend(Protocol):
    """Injected seam over `stacker.gh` so PR-creation paths are testable.

    Production impl (`GhCliBackend`) shells out to `gh`; tests swap in a
    recording fake.
    """

    def scoped(self, *, env: dict[str, str]) -> PRBackend: ...

    def repo_info(self, *, cwd: Path | None = None, repo: str | None = None) -> gh.RepoInfo: ...

    def list_open_prs(
        self,
        repo: str,
        *,
        head: str | None = None,
        search: str | None = None,
    ) -> list[gh.PullRequest]: ...

    def create_pr(self, request: gh.CreatePRRequest) -> str: ...

    def edit_pr(self, request: gh.EditPRRequest) -> None: ...

    def view_pr(self, url: str) -> gh.PullRequest | None: ...

    def search_prs(self, query: str) -> list[gh.PullRequest]: ...

    def batch_pr_review(
        self,
        entries: Sequence[tuple[str, str, int]],
    ) -> dict[tuple[str, str, int], gh.PRReviewSummary]: ...


@dataclass(frozen=True)
class GhCliBackend:
    """Production backend — delegates to `stacker.gh` module-level functions."""

    env: dict[str, str] | None = None

    def scoped(self, *, env: dict[str, str]) -> PRBackend:
        merged = dict(self.env or {})
        merged.update(env)
        return GhCliBackend(env=merged)

    def repo_info(self, *, cwd: Path | None = None, repo: str | None = None) -> gh.RepoInfo:
        return gh.repo_info(cwd=cwd, repo=repo, env=self.env)

    def list_open_prs(
        self,
        repo: str,
        *,
        head: str | None = None,
        search: str | None = None,
    ) -> list[gh.PullRequest]:
        return gh.list_open_prs(repo, head=head, search=search, env=self.env)

    def create_pr(self, request: gh.CreatePRRequest) -> str:
        return gh.create_pr(request, env=self.env)

    def edit_pr(self, request: gh.EditPRRequest) -> None:
        gh.edit_pr(request, env=self.env)

    def view_pr(self, url: str) -> gh.PullRequest | None:
        return gh.view_pr(url, env=self.env)

    def search_prs(self, query: str) -> list[gh.PullRequest]:
        return gh.search_prs(query, env=self.env)

    def batch_pr_review(
        self,
        entries: Sequence[tuple[str, str, int]],
    ) -> dict[tuple[str, str, int], gh.PRReviewSummary]:
        return gh.batch_pr_review(entries, env=self.env)
