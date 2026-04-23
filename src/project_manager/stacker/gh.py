from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import git


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
    proc = git.run(cmd, cwd=cwd, check=False)
    if proc.returncode != 0:
        raise git.GitError(_format_failure(cmd, proc))
    payload = json.loads(proc.stdout or "{}")
    owner = payload.get("owner", {})
    owner_login = owner.get("login", "") if isinstance(owner, dict) else str(owner)
    name = payload.get("name", "")
    if not owner_login or not name:
        raise git.GitError("Could not determine GitHub repository owner/name.")
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
    proc = git.run(cmd, check=False)
    if proc.returncode != 0:
        raise git.GitError(_format_failure(cmd, proc))
    payload = json.loads(proc.stdout or "[]")
    return [_to_pr(item) for item in payload]


def create_pr(request: CreatePRRequest) -> str:
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
    proc = git.run(cmd, check=False)
    if proc.returncode != 0:
        raise git.GitError(_format_failure(cmd, proc))
    return proc.stdout.strip()


def edit_pr(request: EditPRRequest) -> None:
    cmd = ["gh", "pr", "edit", str(request.number), "--repo", request.repo]
    if request.title is not None:
        cmd.extend(["--title", request.title])
    if request.body_file is not None:
        cmd.extend(["--body-file", str(request.body_file)])
    if request.base is not None:
        cmd.extend(["--base", request.base])
    proc = git.run(cmd, check=False)
    if proc.returncode != 0:
        raise git.GitError(_format_failure(cmd, proc))


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


def _format_failure(cmd: list[str], proc: subprocess.CompletedProcess[str]) -> str:
    pieces = ["command failed:", " ".join(cmd)]
    if proc.stdout.strip():
        pieces.append(proc.stdout.strip())
    if proc.stderr.strip():
        pieces.append(proc.stderr.strip())
    return "\n".join(pieces)
