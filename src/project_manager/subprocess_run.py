from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Literal

from . import render
from .errors import CommandError
from .render import SubprocessLogLine


class TaggedCompletedProcess(subprocess.CompletedProcess[str]):
    """`CompletedProcess` plus the per-line tagged log captured during the run.

    `log_events` preserves every line read from stdout/stderr in arrival
    order with timestamp, pid, cmd basename and stream label — enough for
    `format_command_failure` to rebuild an attributed failure message
    without re-parsing the raw streams.
    """

    log_events: list[SubprocessLogLine]


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    stream: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess. By default captures stdout/stderr.

    With `stream=True`, each line of the child's stdout/stderr is echoed
    live to *our* stderr with a `[timestamp pid=N cmd] [out|err]` prefix,
    so parallel runs stay distinguishable. Regardless of `stream`, the
    child's raw output is also captured and returned in `proc.stdout` /
    `proc.stderr` for callers that parse output.
    """
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    cwd_str = str(cwd) if cwd is not None else None

    proc = subprocess.Popen(
        cmd,
        cwd=cwd_str,
        env=full_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    cmd_name = Path(cmd[0]).name if cmd else ""
    pid = proc.pid

    stdout_buf: list[str] = []
    stderr_buf: list[str] = []
    events: list[SubprocessLogLine] = []
    events_lock = threading.Lock()

    def pump(
        pipe: IO[str],
        raw_buf: list[str],
        stream_label: Literal["out", "err"],
    ) -> None:
        for line in pipe:
            raw_buf.append(line)
            event = SubprocessLogLine(
                # Keep source timestamps in UTC; render converts to
                # the user-configured display zone at format time.
                timestamp=datetime.now(tz=UTC),
                pid=pid,
                cmd=cmd_name,
                stream=stream_label,
                text=line.rstrip("\n"),
            )
            with events_lock:
                events.append(event)
            if stream:
                render.emit_subprocess_line(event)

    assert proc.stdout is not None
    assert proc.stderr is not None
    t_out = threading.Thread(target=pump, args=(proc.stdout, stdout_buf, "out"))
    t_err = threading.Thread(target=pump, args=(proc.stderr, stderr_buf, "err"))
    t_out.start()
    t_err.start()

    returncode = proc.wait()
    t_out.join()
    t_err.join()

    result = TaggedCompletedProcess(
        args=cmd,
        returncode=returncode,
        stdout="".join(stdout_buf),
        stderr="".join(stderr_buf),
    )
    result.log_events = events

    if check and returncode != 0:
        raise CommandError(format_command_failure(cmd, result))
    return result


async def run_async(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Async sibling of `run` for fanning subprocess work through an event loop.

    Captures stdout/stderr and does not stream — callers that want live
    log pumping should use the sync `run(..., stream=True)` path. Intended
    for read-only git operations spawned in parallel via `asyncio.TaskGroup`
    (see `repo.ls`), where one thread per child would be wasteful.
    """
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    cwd_str = str(cwd) if cwd is not None else None

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd_str,
        env=full_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode() if stdout_bytes else ""
    stderr = stderr_bytes.decode() if stderr_bytes else ""

    result = TaggedCompletedProcess(
        args=cmd,
        returncode=proc.returncode or 0,
        stdout=stdout,
        stderr=stderr,
    )
    result.log_events = []

    if check and result.returncode != 0:
        raise CommandError(format_command_failure(cmd, result))
    return result


def format_command_failure(
    cmd: list[str],
    proc: subprocess.CompletedProcess[str],
) -> str:
    header = f"command failed: {' '.join(cmd)}"
    events: list[SubprocessLogLine] | None = getattr(proc, "log_events", None)
    if events:
        body = render.format_subprocess_lines(events)
        return header + "\n" + body if body else header
    pieces = [header]
    if proc.stdout and proc.stdout.strip():
        pieces.append(proc.stdout.strip())
    if proc.stderr and proc.stderr.strip():
        pieces.append(proc.stderr.strip())
    return "\n".join(pieces)
