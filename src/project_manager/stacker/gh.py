from __future__ import annotations

import json
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
        return _create_pr_graphql(request)
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


def _create_pr_graphql(request: CreatePRRequest) -> str:
    assert request.head_repo is not None
    base_id = _repo_id(request.repo)
    head_id = _repo_id(request.head_repo)
    mutation = (
        "mutation($baseId:ID!,$headId:ID!,$base:String!,$head:String!,"
        "$title:String!,$body:String,$draft:Boolean){"
        "createPullRequest(input:{"
        "repositoryId:$baseId,headRepositoryId:$headId,"
        "baseRefName:$base,headRefName:$head,"
        "title:$title,body:$body,draft:$draft"
        "}){pullRequest{url}}}"
    )
    data = _graphql(
        mutation,
        baseId=base_id,
        headId=head_id,
        base=request.base,
        head=request.head,
        title=request.title,
        body=request.body_file,   # read from file via `-f body=@<path>`
        draft=request.draft,
    )
    return data["createPullRequest"]["pullRequest"]["url"]


def _repo_id(slug: str) -> str:
    owner, name = slug.split("/", 1)
    data = _graphql(
        "query($o:String!,$n:String!){repository(owner:$o,name:$n){id}}",
        o=owner,
        n=name,
    )
    return data["repository"]["id"]


def _graphql(query: str, **variables: object) -> dict:
    """Invoke `gh api graphql` and return the `data` payload.

    Each kwarg becomes a GraphQL variable. Bools are passed via `-F` so gh
    types them correctly; Path values use `-f <name>=@<path>` so gh reads
    the file contents (needed for multi-line PR bodies); everything else is
    a string via `-f`.
    """
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        if isinstance(value, bool):
            cmd.extend(["-F", f"{key}={'true' if value else 'false'}"])
        elif isinstance(value, Path):
            cmd.extend(["-f", f"{key}=@{value}"])
        else:
            cmd.extend(["-f", f"{key}={value}"])
    proc = run(cmd)
    payload = json.loads(proc.stdout or "{}")
    if payload.get("errors"):
        msgs = "; ".join(e.get("message", str(e)) for e in payload["errors"])
        raise CommandError(f"GraphQL error: {msgs}")
    return payload["data"]


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
