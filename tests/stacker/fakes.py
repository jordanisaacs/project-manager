"""Test fakes for the stacker PR backend.

`RecordingPRBackend` records every call and returns canned responses, so
tests can assert on the exact request stacker would have sent to GitHub
without shelling out to `gh`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from project_manager.stacker import gh


@dataclass
class RecordingPRBackend:
    """In-memory stand-in for `GhCliBackend`.

    - `repo_info` returns `default_repo`.
    - `list_open_prs` returns whatever `pr_for` says for `(repo, head)` or
      matches against the explicit `prs` list.
    - `create_pr` and `edit_pr` append to `created` / `edited` with a
      snapshot of the request (including the rendered body so tests can
      assert on the stack block).
    """

    default_repo: gh.RepoInfo = field(
        default_factory=lambda: gh.RepoInfo(
            name_with_owner="acme/widgets", owner="acme", name="widgets"
        )
    )
    prs_by_head: dict[tuple[str, str], gh.PullRequest] = field(default_factory=dict)
    created: list[tuple[gh.CreatePRRequest, str]] = field(default_factory=list)
    edited: list[tuple[gh.EditPRRequest, str | None]] = field(default_factory=list)
    next_pr_number: int = 1
    next_pr_url_prefix: str = "https://github.com/acme/widgets/pull/"

    def repo_info(
        self,
        *,
        cwd: Path | None = None,  # noqa: ARG002 (Protocol signature)
        repo: str | None = None,  # noqa: ARG002 (Protocol signature)
    ) -> gh.RepoInfo:
        return self.default_repo

    def list_open_prs(
        self,
        repo: str,
        *,
        head: str | None = None,
        search: str | None = None,
    ) -> list[gh.PullRequest]:
        key_head = head
        if search and key_head is None:
            # Recording backend treats forks identically to same-repo lookups;
            # tests set `prs_by_head` using the unqualified branch name.
            key_head = search.rsplit(":", 1)[-1]
        if key_head is None:
            return []
        pr = self.prs_by_head.get((repo, key_head))
        return [pr] if pr else []

    def create_pr(self, request: gh.CreatePRRequest) -> str:
        body = _read_body(request.body_file)
        self.created.append((request, body))
        number = self.next_pr_number
        self.next_pr_number += 1
        url = f"{self.next_pr_url_prefix}{number}"
        # Register the just-created PR so the follow-up `_find_open_pr` sees it.
        head = request.head.rsplit(":", 1)[-1]
        self.prs_by_head[(request.repo, head)] = gh.PullRequest(
            number=number,
            url=url,
            title=request.title,
            body=body,
            head_ref_name=head,
            base_ref_name=request.base,
            state="OPEN",
            is_draft=request.draft,
        )
        return url

    def edit_pr(self, request: gh.EditPRRequest) -> None:
        body = _read_body(request.body_file) if request.body_file else None
        self.edited.append((request, body))

    def view_pr(self, url: str) -> gh.PullRequest | None:
        for pr in self.prs_by_head.values():
            if pr.url == url:
                return pr
        return None

    def search_prs(self, query: str) -> list[gh.PullRequest]:
        """Minimal `search` that honors `repo:` / `head:` / `is:open` tokens."""
        tokens = dict(_parse_search(query))
        repo = tokens.get("repo")
        head = tokens.get("head")
        want_open = tokens.get("is") == "open"
        out: list[gh.PullRequest] = []
        for (pr_repo, pr_head), pr in self.prs_by_head.items():
            if repo and pr_repo != repo:
                continue
            if head and pr_head != head:
                continue
            if want_open and pr.state != "OPEN":
                continue
            out.append(pr)
        return out

    # Convenience helpers for assertions --------------------------------

    def body_sent_for(self, branch: str) -> str | None:
        """Return the most recent body written for `branch` via create or edit."""
        for pr_request, body in reversed(self.created):
            head = pr_request.head.rsplit(":", 1)[-1]
            if head == branch:
                return body
        for req, body in reversed(self.edited):
            pr = _find_pr_by_number(self.prs_by_head, req.number)
            if pr and pr.head_ref_name == branch and body is not None:
                return body
        return None


def _read_body(path: Path) -> str:
    return Path(path).read_text()


def _parse_search(query: str) -> list[tuple[str, str]]:
    """Split `key:value key:value ...` into (key, value) pairs."""
    out: list[tuple[str, str]] = []
    for token in query.split():
        if ":" not in token:
            continue
        key, value = token.split(":", 1)
        out.append((key, value))
    return out


def _find_pr_by_number(
    prs: dict[tuple[str, str], gh.PullRequest], number: int
) -> gh.PullRequest | None:
    for pr in prs.values():
        if pr.number == number:
            return pr
    return None
