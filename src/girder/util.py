"""Small shared helpers: timestamps, ids, subprocess utilities."""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path


def utcnow_iso() -> str:
    """UTC timestamp, ISO-8601, second precision — canonical DB timestamp format."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def new_id() -> str:
    """Fresh UUID4 hex identifier for a row id."""
    return uuid.uuid4().hex


class CommandError(RuntimeError):
    """A host-side subprocess exited nonzero."""

    def __init__(self, argv: list[str], code: int, stderr: str) -> None:
        super().__init__(f"{' '.join(argv[:3])}… exited {code}: {stderr.strip()[:500]}")
        self.argv = argv
        self.code = code
        self.stderr = stderr


async def run_host_cmd(
    argv: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout_s: float | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a host command asynchronously, capturing text output.

    The orchestrator always drives git / container CLIs from its own process —
    this is the only subprocess helper the host side should use. ``env`` is
    merged over ``os.environ`` (used e.g. for GIT_ASKPASS so tokens never
    appear in argv).
    """
    merged_env = {**os.environ, **env} if env else None
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=merged_env,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        await proc.communicate()
        raise
    stdout, stderr = stdout_b.decode(errors="replace"), stderr_b.decode(errors="replace")
    code = proc.returncode if proc.returncode is not None else -1
    if check and code != 0:
        raise CommandError(argv, code, stderr)
    return subprocess.CompletedProcess(argv, code, stdout, stderr)
