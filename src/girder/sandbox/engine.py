"""Sandbox engine interface (impl-plan §6.4).

The engine is driven exclusively by the orchestrator process from the host —
agents have no path to reach it. Concrete PodmanEngine (or DockerEngine via
the same flag surface) lives in :mod:`girder.sandbox.podman`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ContainerSpec:
    """Everything the host needs to start a sandbox. Zero host secrets."""

    name: str
    image: str
    worktree: Path | None = None  # bind-mounted RW at /workspace
    # (pristine host snapshot, relative path inside the worktree it shadows):
    # Layer-1 test immunity (§8.2) — mounted RO *before* the container starts.
    test_snapshot: tuple[Path, str] | None = None
    ro_mounts: dict[str, str] = field(default_factory=dict)  # host path -> in-container
    network: str = "none"  # none | private
    memory: str = "4g"
    cpus: float = 2.0
    pids_limit: int = 512
    workdir: str = "/workspace"
    # In-container user for the agent. ``None`` lets the engine pick the
    # mapping that equals the worktree's owning host uid (docker: --user;
    # podman: --userns=keep-id) — with CAP_DAC_OVERRIDE dropped, "guest root"
    # has no special write access, so the agent must literally *be* the owner.
    run_as: str | None = None


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class SandboxTimeout(Exception):
    """Orchestrator-side wall-clock timeout fired; the container was killed."""


class SandboxEngine(ABC):
    @abstractmethod
    async def start(self, spec: ContainerSpec) -> str:
        """Start a detached sandbox; returns its name/id."""

    @abstractmethod
    async def exec(
        self, name: str, cmd: list[str], *, timeout_s: float = 120.0, user: str | None = None
    ) -> ExecResult:
        """Run *cmd* inside the sandbox with an orchestrator-side timeout.

        On timeout the whole container is killed (never trust an in-guest
        timer) and :class:`SandboxTimeout` is raised. ``user`` overrides the
        in-container user for this exec only (e.g. ``"0"`` to prove that even
        guest root cannot bypass the sandbox).
        """

    @abstractmethod
    async def kill(self, name: str) -> None:
        """Kill the sandbox; idempotent."""

    @abstractmethod
    async def exists(self, name: str) -> bool:
        """True while the sandbox is running."""

    async def stream_logs(self, sandbox_id: str) -> AsyncIterator[str]:
        """Yield sandbox log lines as they arrive (impl-plan §6.4).

        Follows the log until the sandbox exits or the consumer breaks out of
        the iterator — breaking cancels the underlying ``logs --follow`` and
        terminates the streaming subprocess. Default is an empty stream;
        engines that have a log source (PodmanEngine) override this. Kept
        non-abstract so lightweight fakes remain concrete.
        """
        raise NotImplementedError
        yield ""  # pragma: no cover — makes this an async-generator method
