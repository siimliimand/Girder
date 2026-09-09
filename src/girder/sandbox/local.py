"""LOCAL EXECUTION SANDBOX — NOT A SECURITY BOUNDARY. AT ALL.

``LocalExecSandbox`` runs every ``exec`` argv **directly on the host** with
``cwd`` set to the spec's worktree directory. There is no isolation
whatsoever: no capability dropping, no filesystem confinement, no network
blocking, no user remapping. Container-style ``/workspace/...`` path
arguments are rewritten to the host worktree so tool payloads stay
engine-agnostic.

It exists exclusively for:

* the unit/integration test-suite (so tests never need podman), and
* explicit opt-in development use via ``girder daemon --sandbox local`` /
  ``girder pump --sandbox local``.

NEVER use it to execute untrusted model output in any setting that matters.
Production runs use :class:`girder.sandbox.podman.PodmanEngine`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from girder.sandbox.engine import (
    ContainerSpec,
    ExecResult,
    SandboxEngine,
    SandboxTimeout,
)

_WORKSPACE_PREFIX = "/workspace"


class LocalExecSandbox(SandboxEngine):
    """Host-side stand-in mirroring :class:`PodmanEngine`'s observable contract.

    Timeout contract (must match podman.py exactly): on timeout the executed
    process is killed, then :class:`SandboxTimeout` is raised — callers never
    see a ``timed_out`` flag from this engine.
    """

    def __init__(self, worktree_hint: Path | None = None) -> None:
        self._worktree_hint = worktree_hint
        self._specs: dict[str, ContainerSpec] = {}
        self.started: list[ContainerSpec] = []
        self.killed: list[str] = []

    # ------------------------------------------------------------------ start

    async def start(self, spec: ContainerSpec) -> str:
        self._specs[spec.name] = spec
        self.started.append(spec)
        return spec.name

    def worktree_of(self, name: str) -> Path:
        """Host directory commands for *name* run in (tests introspect this)."""
        spec = self._specs[name]
        return spec.worktree or self._worktree_hint or Path.cwd()

    # ------------------------------------------------------------------- exec

    def _rewrite(self, arg: str, spec: ContainerSpec) -> str:
        """Map container-style ``/workspace/...`` argv elements to the host."""
        if spec.worktree is None or not arg.startswith(_WORKSPACE_PREFIX):
            return arg
        rest = arg[len(_WORKSPACE_PREFIX) :].lstrip("/")
        if not rest:
            return str(spec.worktree)
        return str(spec.worktree / rest)

    async def exec(
        self, name: str, cmd: list[str], *, timeout_s: float = 120.0, user: str | None = None
    ) -> ExecResult:
        spec = self._specs.get(name)
        if spec is None:
            raise KeyError(f"no such sandbox: {name!r} (start() it first)")
        argv = [self._rewrite(arg, spec) for arg in cmd]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(self.worktree_of(name)),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise SandboxTimeout(
                f"exec timed out after {timeout_s}s (process killed): {' '.join(cmd[:3])}"
            ) from None
        return ExecResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout_b.decode(errors="replace"),
            stderr=stderr_b.decode(errors="replace"),
        )

    # ------------------------------------------------------------ kill/exists

    async def kill(self, name: str) -> None:
        self._specs.pop(name, None)
        self.killed.append(name)

    async def exists(self, name: str) -> bool:
        return name in self._specs
