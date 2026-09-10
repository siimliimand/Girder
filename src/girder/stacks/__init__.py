"""Stack plugin architecture (improvements plan WP 11.1).

A *stack* bundles everything the orchestrator needs to know about a target
project's language toolchain:

* the runner container image (:meth:`StackPlugin.runner_image`),
* how to run the full test suite and emit JUnit XML
  (:meth:`StackPlugin.test_command`),
* how to list symbols in a source file in-container
  (:meth:`StackPlugin.symbol_outline_command` — consumed from WP 11.4, wave 2),
* which files signal test changes (Layer-2 hash manifest),
* which host-side package caches to bind read-only into attempts.

Built-in plugins register themselves in :data:`STACK_REGISTRY` at import
time; ``load_settings()`` validates ``project.stack`` against its keys, so an
unknown stack is a hard configuration error, never a silent Python default.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class UnknownStackError(ValueError):
    """``project.stack`` names no registered stack plugin."""


class StackPlugin(ABC):
    """Per-language toolchain contract (ws07 WP 11.1)."""

    name: str  # e.g. "python-3.12", "node-20", "go-1.23"

    @abstractmethod
    def runner_image(self) -> str:
        """Docker/Podman image tag for this stack's runner."""

    @abstractmethod
    def test_command(self, python_bin: str | None = None) -> list[str]:
        """Command to run the full test suite and produce JUnit XML.

        The JUnit report must land at ``/workspace/.girder-verify.xml`` inside
        the container — the verify loop and baselines parse that exact path.
        ``python_bin`` only matters for the Python stack (interpreter
        override); other stacks ignore it.
        """

    @abstractmethod
    def symbol_outline_command(self, path: str) -> list[str]:
        """In-container command to list symbols in a source file."""

    @abstractmethod
    def test_signal_patterns(self) -> list[str]:
        """Glob patterns for test files (Layer-2 hash manifest)."""

    @abstractmethod
    def package_cache_mounts(self) -> dict[str, str]:
        """Host path → container path for read-only package cache mounts."""


# Populated at import time by the built-in plugins below.
STACK_REGISTRY: dict[str, StackPlugin] = {}


def register(plugin: StackPlugin) -> StackPlugin:
    """Add *plugin* to the registry (import-time hook for built-ins)."""
    STACK_REGISTRY[plugin.name] = plugin
    return plugin


def get_stack(name: str) -> StackPlugin:
    """Resolve a stack name to its plugin; unknown names are a hard error."""
    try:
        return STACK_REGISTRY[name]
    except KeyError:
        raise UnknownStackError(
            f"unknown project.stack {name!r}; known stacks: {sorted(STACK_REGISTRY)}"
        ) from None


# Built-ins. Import order is irrelevant — registration is idempotent by name.
from girder.stacks.golang import GoPlugin  # noqa: E402
from girder.stacks.node import NodePlugin  # noqa: E402
from girder.stacks.python import PythonPlugin  # noqa: E402

register(GoPlugin())
register(NodePlugin())
register(PythonPlugin())
