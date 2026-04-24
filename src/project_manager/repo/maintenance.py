"""`pm repo maintenance`: keep the git object store + index + fsmonitor warm.

Per repo (on the canonical checkout):
  - `git maintenance run` with an explicit task list that covers object-db
    health (commit-graph, loose-objects, incremental-repack, pack-refs) but
    excludes `prefetch` — fetching is `pm repo pull`'s job.
  - `git worktree prune` — drop records of gitdirs whose worktree is gone.

Per target (canonical repo + every pool slot):
  - `git update-index --refresh` — warms git's stat cache and, on
    fsmonitor-enabled repos, drives the watchman handshake so cold
    `pm stacker ls` / `git status` calls don't pay the initial-crawl cost.

Intended to run on a timer. No subprocess timeout: watchman's initial
crawl on a universe-scale worktree can legitimately take minutes, and
a wedged timer run is visible in the next invocation's output, whereas
a spurious timeout would just discard the work done so far.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from project_manager.async_util import bounded_gather
from project_manager.config import Concurrency
from project_manager.paths import Paths
from project_manager.pool import slot as slot_mod
from project_manager.render import Column, emit_command_start
from project_manager.repo import discovery
from project_manager.subprocess_run import run

# Tasks passed to `git maintenance run --task=…`. Order mirrors git's own
# default sequence; `prefetch` is omitted intentionally — see module docstring.
_MAINTENANCE_TASKS = (
    "commit-graph",
    "loose-objects",
    "incremental-repack",
    "pack-refs",
)


@dataclass(frozen=True)
class MaintenanceResult:
    repo: str
    # "(repo)" for the canonical checkout, slot uuid for pool slots.
    target: str
    op: str  # "maintenance" | "worktree-prune" | "update-index"
    ok: bool
    message: str


@dataclass(frozen=True)
class _Job:
    repo: str
    target: str
    op: str
    cmd: list[str]
    # `update-index --refresh` exits 1 when files need refreshing — that's
    # informational, not a failure. Ops that set this flag report ok=True
    # regardless of exit status.
    ignore_returncode: bool = False


COLUMNS: list[Column] = [
    Column("Repo", "repo", style="blue"),
    Column("Target", "target", style="dim"),
    Column("Op", "op"),
    Column(
        "Result",
        lambda r: "ok" if r.ok else "fail",
        style=lambda r: "green" if r.ok else "red",
    ),
    Column("Message", "message"),
]


async def _run_job(job: _Job) -> MaintenanceResult:
    emit_command_start(job.cmd)
    # Use the sync `run(stream=True)` path — it already pumps each
    # subprocess line to stderr with the `[timestamp pid=N cmd]
    # [out|err] …` prefix, so concurrent children stay distinguishable
    # and `pm repo maintenance` on a timer produces a useful live log.
    # `asyncio.to_thread` keeps the event loop free while the sync call
    # blocks on wait(). `bounded_gather` in the caller handles the
    # concurrency cap — no per-job semaphore here.
    # TODO: teach `run_async` to stream natively (pump via asyncio
    # StreamReader) so we don't need a thread per concurrent child.
    # The existing async path uses `proc.communicate()` which waits for
    # EOF before returning anything — adding a line-pump mode there
    # would let us drop `asyncio.to_thread` and the per-job thread.
    result = await asyncio.to_thread(
        run, job.cmd, check=False, stream=True,
    )
    ok = job.ignore_returncode or result.returncode == 0
    # Many maintenance subcommands are silent on success, so fall back to
    # "ok"/"exit N" when git said nothing useful.
    tail = [line for line in result.stderr.splitlines() if line.strip()]
    message = (
        tail[-1].strip() if tail
        else ("ok" if ok else f"exit {result.returncode}")
    )
    return MaintenanceResult(
        repo=job.repo, target=job.target, op=job.op, ok=ok, message=message,
    )


def _update_index_targets(
    paths: Paths, repo: str,
) -> list[tuple[str, Path]]:
    """`(target_label, path)` for the canonical repo + every pool slot."""
    out: list[tuple[str, Path]] = []
    repo_dir = paths.repo(repo)
    if repo_dir.is_dir():
        out.append(("(repo)", repo_dir))
    out.extend((s.uuid, s.path) for s in slot_mod.list_slots(paths, repo))
    return out


def _jobs_for_repo(paths: Paths, repo: str) -> list[_Job]:
    jobs: list[_Job] = []
    repo_dir = paths.repo(repo)
    if repo_dir.is_dir():
        task_args = [f"--task={t}" for t in _MAINTENANCE_TASKS]
        jobs.append(_Job(
            repo=repo, target="(repo)", op="maintenance",
            cmd=["git", "-C", str(repo_dir), "maintenance", "run", *task_args],
        ))
        jobs.append(_Job(
            repo=repo, target="(repo)", op="worktree-prune",
            cmd=["git", "-C", str(repo_dir), "worktree", "prune"],
        ))
    for target, path in _update_index_targets(paths, repo):
        jobs.append(_Job(
            repo=repo, target=target, op="update-index",
            cmd=["git", "-C", str(path), "update-index", "-q", "--refresh"],
            ignore_returncode=True,
        ))
    return jobs


async def _maintain_async(
    paths: Paths, repos: list[str], limit: int,
) -> list[MaintenanceResult]:
    jobs = [j for r in repos for j in _jobs_for_repo(paths, r)]
    return await bounded_gather(
        (_run_job(j) for j in jobs), limit=limit,
    )


def maintain(
    paths: Paths,
    repos: list[str] | None,
    cfg: Concurrency,
) -> list[MaintenanceResult]:
    """Run maintenance + index/fsmonitor warmup for each selected repo.

    `repos=None` means every canonical repo under `paths.repos`. Unknown
    repo names are a hard ValueError before any subprocess runs.
    """
    known = set(discovery.list_repos(paths))
    selected = sorted(known) if repos is None else repos
    unknown = [r for r in selected if r not in known]
    if unknown:
        raise ValueError(f"unknown repo(s): {', '.join(unknown)}")
    if not selected:
        return []
    return asyncio.run(_maintain_async(paths, selected, cfg.limit))
