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
) -> subprocess.CompletedProcess[str]:
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        env=full_env,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise CommandError(format_command_failure(cmd, proc))
    return proc


def format_command_failure(cmd: list[str], proc: subprocess.CompletedProcess[str]) -> str:
    pieces = ["command failed:", " ".join(cmd)]
    if proc.stdout.strip():
        pieces.append(proc.stdout.strip())
    if proc.stderr.strip():
        pieces.append(proc.stderr.strip())
    return "\n".join(pieces)
