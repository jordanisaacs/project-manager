from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from project_manager.errors import CommandError
from project_manager.subprocess_run import run


@dataclass(frozen=True)
class RepoInfo:
    name_with_owner: str
    owner: str
    name: str


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    title: str
    body: str
    head_ref_name: str
    base_ref_name: str
    state: str
    is_draft: bool


@dataclass(frozen=True)
class CreatePRRequest:
    repo: str
    base: str
    head: str
    title: str
    body_file: Path
    draft: bool
    # When `head_repo` is set and differs from `repo`, the PR is created via
    # GraphQL so that cross-repo same-owner forks (e.g. databricks-eng/universe
    # ← databricks-eng/universe-dev) work. `gh pr create` cannot do this:
    # https://github.com/cli/cli/issues/10093
    head_repo: str | None = None


@dataclass(frozen=True)
class EditPRRequest:
    repo: str
    number: int
    title: str | None = None
    body_file: Path | None = None
    base: str | None = None


def repo_info(*, cwd: Path | None = None, repo: str | None = None) -> RepoInfo:
    cmd = ["gh", "repo", "view", "--json", "name,owner"]
    if repo:
        cmd.extend(["--repo", repo])
    proc = run(cmd, cwd=cwd)
    payload = json.loads(proc.stdout or "{}")
    owner = payload.get("owner", {})
    owner_login = owner.get("login", "") if isinstance(owner, dict) else str(owner)
    name = payload.get("name", "")
    if not owner_login or not name:
        raise CommandError("Could not determine GitHub repository owner/name.")
    return RepoInfo(name_with_owner=f"{owner_login}/{name}", owner=owner_login, name=name)


def list_open_prs(
    repo: str, *, head: str | None = None, search: str | None = None
) -> list[PullRequest]:
    cmd = [
        "gh", "pr", "list",
        "--repo", repo,
        "--state", "open",
        "--json",
        "number,url,title,body,headRefName,baseRefName,state,isDraft",
    ]
    if head:
        cmd.extend(["--head", head])
    if search:
        cmd.extend(["--search", search])
    proc = run(cmd)
    payload = json.loads(proc.stdout or "[]")
    return [_to_pr(item) for item in payload]


def create_pr(request: CreatePRRequest) -> str:
    if request.head_repo and request.head_repo != request.repo:
        return _create_pr_rest(request)
    cmd = [
        "gh", "pr", "create",
        "--repo", request.repo,
        "--base", request.base,
        "--head", request.head,
        "--title", request.title,
        "--body-file", str(request.body_file),
    ]
    if request.draft:
        cmd.append("--draft")
    return run(cmd).stdout.strip()


def _create_pr_rest(request: CreatePRRequest) -> str:
    """Create a cross-fork same-owner PR via the REST API.

    Matches how universe's `ci/gitstack` does it (octocrab: `pulls.create(...)
    .head_repo(repo)`): the REST field `head_repo` is what `gh pr create`
    never exposes. Pass it and GitHub resolves the head branch on that repo.
    """
    assert request.head_repo is not None
    cmd = [
        "gh", "api", "--method", "POST",
        f"/repos/{request.repo}/pulls",
        "-f", f"title={request.title}",
        "-f", f"body={request.body_file.read_text()}",
        "-f", f"head={request.head}",
        "-f", f"head_repo={request.head_repo}",
        "-f", f"base={request.base}",
        "-F", f"draft={'true' if request.draft else 'false'}",
    ]
    proc = run(cmd)
    payload = json.loads(proc.stdout or "{}")
    url = payload.get("html_url")
    if not url:
        raise CommandError(f"createPullRequest returned no url: {payload!r}")
    return url


_PR_URL_RE = re.compile(
    r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?$"
)


def parse_pr_url(url: str) -> tuple[str, str, int] | None:
    """Extract (owner, repo, number) from a GitHub PR URL, or None."""
    match = _PR_URL_RE.match(url)
    if match is None:
        return None
    return match.group(1), match.group(2), int(match.group(3))


def search_prs(query: str) -> list[PullRequest]:
    """Search PRs via the GitHub GraphQL `search` endpoint.

    Same path universe gitstack uses (`octocrab_client.rs:171-225`):
    `gh pr list --search` is REST-backed and misses cross-fork same-owner
    PRs; GraphQL search does not. Query format mirrors universe's:
    `repo:{owner}/{repo} head:{branch} is:pr is:open`.
    """
    graphql_query = (
        "query($q:String!){search(query:$q,type:ISSUE,first:25){"
        "nodes{... on PullRequest{"
        "number url title body state isDraft "
        "headRefName baseRefName"
        "}}}}"
    )
    proc = run(
        ["gh", "api", "graphql", "-f", f"query={graphql_query}", "-f", f"q={query}"],
    )
    payload = json.loads(proc.stdout or "{}")
    if payload.get("errors"):
        msgs = "; ".join(e.get("message", str(e)) for e in payload["errors"])
        raise CommandError(f"GraphQL error: {msgs}")
    nodes = payload.get("data", {}).get("search", {}).get("nodes", []) or []
    return [_graphql_pr(node) for node in nodes if node]


def _graphql_pr(node: dict) -> PullRequest:
    return PullRequest(
        number=int(node["number"]),
        url=node.get("url", ""),
        title=node.get("title", ""),
        body=node.get("body") or "",
        head_ref_name=node.get("headRefName", ""),
        base_ref_name=node.get("baseRefName", ""),
        state=str(node.get("state", "")).upper(),
        is_draft=bool(node.get("isDraft", False)),
    )


def view_pr(url: str) -> PullRequest | None:
    """Fetch the current state of a PR by URL. None if the PR can't be found.

    Stacker caches PR URLs on `tracked_branches` (mirroring universe
    gitstack's `StackItem.pr`) and calls this on subsequent runs instead
    of re-running a search. Uses REST `GET /repos/{o}/{r}/pulls/{n}` so
    the fetch is a single round-trip by known ID — no dependency on
    GitHub's search index propagation.
    """
    parsed = parse_pr_url(url)
    if parsed is None:
        return None
    owner, repo, number = parsed
    proc = run(
        ["gh", "api", f"/repos/{owner}/{repo}/pulls/{number}"], check=False,
    )
    if proc.returncode != 0:
        return None
    payload = json.loads(proc.stdout or "{}")
    if "number" not in payload:
        return None
    return _rest_pr(payload)


def _rest_pr(item: dict) -> PullRequest:
    """Shape a REST `pull_request` payload into our PullRequest dataclass.

    Normalizes the state field so OPEN/MERGED/CLOSED behaves like the
    `gh pr list --json state` output regardless of which endpoint we
    used.
    """
    head = item.get("head") or {}
    base = item.get("base") or {}
    state_raw = item.get("state", "open")
    state = "MERGED" if item.get("merged") else state_raw.upper()
    return PullRequest(
        number=int(item["number"]),
        url=item.get("html_url", ""),
        title=item.get("title", ""),
        body=item.get("body") or "",
        head_ref_name=head.get("ref", ""),
        base_ref_name=base.get("ref", ""),
        state=state,
        is_draft=bool(item.get("draft", False)),
    )


@dataclass(frozen=True)
class PRReviewSummary:
    """Live-review state for a single PR.

    Populated by one GraphQL call per (owner, repo) set; consumed by the
    renderer to upgrade the offline `● / ○` icons to the full
    `▼ ⚑ ✓ ◼` set gitstack ships.
    """

    state: str               # OPEN | MERGED | CLOSED
    is_draft: bool
    is_approved: bool
    has_open_comments: bool


def batch_pr_review(
    entries: Sequence[tuple[str, str, int]],
) -> dict[tuple[str, str, int], PRReviewSummary]:
    """Bulk-fetch review state for the PRs in `entries` (owner, repo, number).

    One GraphQL call per unique (owner, repo) group — GitHub's API only
    lets us query aliased fields inside one `repository(owner, name)`
    selection. Inside each group we project per-PR sub-selections so the
    whole set comes back in a single request.

    Review semantics (mirroring gitstack):
    - `is_approved`: any `latestReviews` node with `state == APPROVED`.
    - `has_open_comments`: any node with `state == CHANGES_REQUESTED`.
    - `is_draft`: PR-level `isDraft`.
    - `state`: `MERGED` when `merged: true`, else pass-through.
    """
    out: dict[tuple[str, str, int], PRReviewSummary] = {}
    by_repo: dict[tuple[str, str], list[int]] = {}
    for owner, repo, number in entries:
        by_repo.setdefault((owner, repo), []).append(number)
    for (owner, repo), numbers in by_repo.items():
        if not numbers:
            continue
        alias_body = "\n".join(
            f"pr_{n}: pullRequest(number: {n}) {{ state isDraft merged "
            f"latestReviews(last: 50) {{ nodes {{ state }} }} }}"
            for n in numbers
        )
        query = (
            f'query {{ repository(owner: "{owner}", name: "{repo}") {{ '
            f'{alias_body} }} }}'
        )
        proc = run(
            ["gh", "api", "graphql", "-f", f"query={query}"], check=False,
        )
        if proc.returncode != 0:
            continue
        payload = json.loads(proc.stdout or "{}")
        repo_node = (payload.get("data") or {}).get("repository") or {}
        for number in numbers:
            node = repo_node.get(f"pr_{number}")
            if not node:
                continue
            out[(owner, repo, number)] = _graphql_review(node)
    return out


def _graphql_review(node: dict) -> PRReviewSummary:
    reviews = ((node.get("latestReviews") or {}).get("nodes")) or []
    states = [(r.get("state") or "").upper() for r in reviews if r]
    state_raw = str(node.get("state") or "").upper()
    return PRReviewSummary(
        state="MERGED" if node.get("merged") else state_raw,
        is_draft=bool(node.get("isDraft", False)),
        is_approved="APPROVED" in states,
        has_open_comments="CHANGES_REQUESTED" in states,
    )


def edit_pr(request: EditPRRequest) -> None:
    cmd = ["gh", "pr", "edit", str(request.number), "--repo", request.repo]
    if request.title is not None:
        cmd.extend(["--title", request.title])
    if request.body_file is not None:
        cmd.extend(["--body-file", str(request.body_file)])
    if request.base is not None:
        cmd.extend(["--base", request.base])
    run(cmd)


def _to_pr(item: dict) -> PullRequest:
    return PullRequest(
        number=int(item["number"]),
        url=item["url"],
        title=item.get("title", ""),
        body=item.get("body", ""),
        head_ref_name=item.get("headRefName", ""),
        base_ref_name=item.get("baseRefName", ""),
        state=item.get("state", ""),
        is_draft=bool(item.get("isDraft", False)),
    )
