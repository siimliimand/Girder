"""Stack plugin architecture (WS-07 Part A, WP 11.1).

Each supported language stack is a :class:`StackPlugin`: the single source of
truth for that stack's runner image, verify command, symbol-outline command,
test-file glob patterns, and package-cache mounts. The registry is populated
at import time; ``Settings`` validation rejects any ``project.stack`` value
that is not a registry key.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

# JUnit report filename the verify loop expects inside the worktree. Shared
# by every stack: the report is orchestrator plumbing, not stack output.
VERIFY_XML = ".girder-verify.xml"


class StackPlugin(ABC):
    """One supported language stack (WS-07 WP 11.1)."""

    name: str  # e.g. "python-3.12", "node-20", "go-1.23"

    @abstractmethod
    def runner_image(self) -> str:
        """Docker/Podman image tag for this stack's runner."""

    @abstractmethod
    def test_command(self, python_bin: str | None = None) -> list[str]:
        """Command to run the full test suite and produce JUnit XML."""

    @abstractmethod
    def symbol_outline_command(self, path: str) -> list[str]:
        """In-container command to list symbols in a source file."""

    @abstractmethod
    def test_signal_patterns(self) -> list[str]:
        """Glob patterns for test files (Layer-2 hash manifest)."""

    @abstractmethod
    def package_cache_mounts(self) -> dict[str, str]:
        """Host path -> container path for read-only package cache mounts."""


from girder.stacks.python import PythonPlugin  # noqa: E402

STACK_REGISTRY: dict[str, StackPlugin] = {
    PythonPlugin.name: PythonPlugin(),
}
