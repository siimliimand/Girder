"""Rootless-container sandbox (Podman preferred; identical Docker fallback).

Every mechanical property of the sandbox is expressed in the ``run`` argv the
*orchestrator* builds — the agent cannot influence any of it:

* ``--cap-drop=ALL --security-opt no-new-privileges`` — in-guest root (uid
  mapped) has no ``CAP_SYS_ADMIN``, so the read-only test-snapshot bind mount
  cannot be remounted writable (plan.md §8.2 Layer 1).
* ``--network none`` by default — dependencies come from RO cache mounts and
  the pre-baked runner image, never the internet (§8.1).
* the test snapshot shadows the worktree's own test directory at the same
  path: writes there fail with ``EROFS`` even though ``/workspace`` is RW.
* timeouts are enforced *here*, orchestrator-side, by killing the container.

Podman and Docker share this flag surface; the runtime is selected by config
(``[sandbox] runtime``) with automatic fallback to whichever exists.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from girder.sandbox.engine import (
    ContainerSpec,
    ExecResult,
    SandboxEngine,
    SandboxTimeout,
)
from girder.util import CommandError, run_host_cmd

_RUNTIMES = ("podman", "docker")


def detect_runtime(preferred: str = "podman") -> str:
    """Return the first available container runtime, preferring *preferred*."""
    order = [preferred, *(r for r in _RUNTIMES if r != preferred)]
    for runtime in order:
        if shutil.which(runtime):
            return runtime
    raise RuntimeError("no container runtime found: install rootless podman (or docker)")


class PodmanEngine(SandboxEngine):
    """Concrete engine; the CLI binary is podman or docker — same flags."""

    def __init__(self, runtime: str | None = None) -> None:
        self.runtime = runtime or detect_runtime()

    # ------------------------------------------------------------------ start

    def _run_argv(self, spec: ContainerSpec) -> list[str]:
        argv = [
            self.runtime,
            "run",
            "--detach",
            "--rm",
            "--name",
            spec.name,
            "--cap-drop=ALL",
            "--security-opt",
            "no-new-privileges",
            "--network",
            spec.network if spec.network != "private" else "bridge",
            "--pids-limit",
            str(spec.pids_limit),
            "--memory",
            spec.memory,
            "--cpus",
            str(spec.cpus),
        ]
        for host, container in spec.ro_mounts.items():
            argv += ["-v", f"{host}:{container}:ro"]
        if spec.worktree is not None:
            argv += ["-v", f"{spec.worktree}:/workspace"]
        if spec.test_snapshot is not None:
            snapshot, rel = spec.test_snapshot
            rel = rel.strip("/")
            argv += ["-v", f"{snapshot}:/workspace/{rel}:ro"]
        if spec.run_as:
            argv += ["--user", spec.run_as]
        elif self.runtime == "docker":
            argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
        elif self.runtime == "podman":
            argv += ["--userns=keep-id"]
        if spec.workdir:
            argv += ["-w", spec.workdir]
        argv += [spec.image, "sleep", "infinity"]
        return argv

    async def start(self, spec: ContainerSpec) -> str:
        # trivial stat() checks; not worth a thread hop
        if spec.worktree is not None and not Path(spec.worktree).is_dir():  # noqa: ASYNC240
            raise FileNotFoundError(f"worktree does not exist: {spec.worktree}")
        if spec.test_snapshot is not None and not spec.test_snapshot[0].is_dir():
            raise FileNotFoundError(f"test snapshot does not exist: {spec.test_snapshot[0]}")
        try:
            await run_host_cmd(self._run_argv(spec))
        except CommandError as exc:
            raise RuntimeError(f"failed to start sandbox {spec.name}: {exc}") from exc
        return spec.name

    # ------------------------------------------------------------------- exec

    async def exec(
        self, name: str, cmd: list[str], *, timeout_s: float = 120.0, user: str | None = None
    ) -> ExecResult:
        argv = [self.runtime, "exec"]
        if user:
            argv += ["--user", user]
        argv += [name, *cmd]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            # Never trust an in-guest timer: kill the whole container.
            await self.kill(name)
            raise SandboxTimeout(
                f"exec timed out after {timeout_s}s (container killed): {' '.join(cmd[:3])}"
            ) from None
        return ExecResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_b.decode(errors="replace"),
            stderr=stderr_b.decode(errors="replace"),
        )

    # ------------------------------------------------------------ kill/exists

    async def kill(self, name: str) -> None:
        await run_host_cmd([self.runtime, "kill", name], check=False)

    async def exists(self, name: str) -> bool:
        result = await run_host_cmd(
            [self.runtime, "inspect", "-f", "{{.State.Running}}", name], check=False
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    async def remove(self, name: str) -> None:
        await run_host_cmd([self.runtime, "rm", "-f", name], check=False)
