"""Pure ``run``-argv construction for the sandbox engine (impl-plan §6.4).

Every mechanical property of the sandbox is expressed in the argv the
orchestrator builds (girder/sandbox/podman.py); these tests pin that argv so
hardening flags cannot silently regress — no container runtime required.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from girder.sandbox.engine import ContainerSpec
from girder.sandbox.podman import PodmanEngine

_IMAGE = "girder-runner:python-3.12"


def _argv(runtime: str, **spec_kwargs: Any) -> list[str]:
    spec = ContainerSpec(
        name="girder-test",
        image=_IMAGE,
        worktree=Path("/tmp/wt"),
        **spec_kwargs,
    )
    return PodmanEngine(runtime)._run_argv(spec)


def _flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def test_podman_run_argv_hardening_flags() -> None:
    argv = _argv("podman", pids_limit=777, memory="2g", cpus=1.5)
    assert argv[:3] == ["podman", "run", "--detach"]
    for flag in ("--rm", "--init", "--cap-drop=ALL", "--userns=keep-id"):
        assert flag in argv, flag
    assert _flag(argv, "--security-opt") == "no-new-privileges"
    assert _flag(argv, "--network") == "none"
    assert _flag(argv, "--pids-limit") == "777"
    assert _flag(argv, "--memory") == "2g"
    assert _flag(argv, "--cpus") == "1.5"
    assert _flag(argv, "-v") == "/tmp/wt:/workspace"
    assert _flag(argv, "-w") == "/workspace"
    assert argv[-3] == _IMAGE and argv[-2:] == ["sleep", "infinity"]


def test_docker_pins_worktree_owner_user() -> None:
    """Docker has no keep-id: --user maps to the invoking host uid/gid."""
    argv = _argv("docker")
    assert "--userns" not in argv
    assert _flag(argv, "--user") == f"{os.getuid()}:{os.getgid()}"


def test_run_as_overrides_default_user_mapping() -> None:
    """An explicit run_as (e.g. the layer-1 guest-root probe) replaces it."""
    argv = _argv("podman", run_as="0")
    assert "--userns=keep-id" not in argv
    assert _flag(argv, "--user") == "0"


def test_ro_mounts_and_test_snapshot_mount_read_only() -> None:
    argv = _argv(
        "podman",
        ro_mounts={"/var/cache/orchestrator/pip": "/cache/pip"},
        test_snapshot=(Path("/tmp/snap"), "tests"),
    )
    mounts = [argv[i + 1] for i, v in enumerate(argv) if v == "-v"]
    assert "/tmp/wt:/workspace" in mounts  # the RW workspace
    assert "/var/cache/orchestrator/pip:/cache/pip:ro" in mounts
    assert "/tmp/snap:/workspace/tests:ro" in mounts  # shadows the worktree copy (§8.2)


def test_private_network_maps_to_bridge() -> None:
    """Documented deviation (podman.py header): no loopback-only rootless flag."""
    argv = _argv("podman", network="private")
    assert _flag(argv, "--network") == "bridge"
