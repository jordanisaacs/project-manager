from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .errors import CommandError


def run(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
    stream: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess. By default captures stdout/stderr.

    With `stream=True`, stdout+stderr are inherited from the current process
    so the user sees live output (git push progress, network retries, etc.).
    `proc.stdout` / `proc.stderr` will be None in that mode — only use this
    for commands whose output you don't need to parse.
    """
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    cwd_str = str(cwd) if cwd is not None else None
    if stream:
        proc = subprocess.run(
            cmd, cwd=cwd_str, env=full_env, text=True, check=False,
        )
    else:
        proc = subprocess.run(
            cmd, cwd=cwd_str, env=full_env, text=True,
            capture_output=True, check=False,
        )
    if check and proc.returncode != 0:
        raise CommandError(format_command_failure(cmd, proc))
    return proc


def format_command_failure(cmd: list[str], proc: subprocess.CompletedProcess[str]) -> str:
    pieces = ["command failed:", " ".join(cmd)]
    if proc.stdout and proc.stdout.strip():
        pieces.append(proc.stdout.strip())
    if proc.stderr and proc.stderr.strip():
        pieces.append(proc.stderr.strip())
    return "\n".join(pieces)
