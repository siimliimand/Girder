"""Unit: SandboxEngine.stream_logs (impl-plan §6.4) — argv shape + streaming.

No podman required: the subprocess is faked, and the runtime is pinned so
PodmanEngine() never shells out to detect_runtime().
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from girder.sandbox.engine import ContainerSpec
from girder.sandbox.local import LocalExecSandbox
from girder.sandbox.podman import PodmanEngine


class _FakeStdout:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = iter(lines)

    def __aiter__(self) -> Any:
        return self

    async def __anext__(self) -> bytes:
        try:
            return next(self._lines)
        except StopIteration:
            raise StopAsyncIteration from None


class _FakeProc:
    """Just enough of asyncio.subprocess.Process for stream_logs."""

    def __init__(self, lines: list[bytes]) -> None:
        self.stdout: Any = _FakeStdout(lines)
        self.returncode: int | None = None
        self.argv_used: list[str] = []

    def terminate(self) -> None:
        self.returncode = -15

    async def wait(self) -> int:
        assert self.returncode is not None
        return self.returncode


@pytest.mark.asyncio
async def test_podman_stream_logs_argv_and_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = PodmanEngine(runtime="podman")
    calls: list[list[str]] = []
    proc = _FakeProc([b"hello\n", b"world\n"])

    async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        calls.append(list(argv))
        assert kwargs.get("stdout") == asyncio.subprocess.PIPE
        return proc

    monkeypatch.setattr("girder.sandbox.podman.asyncio.create_subprocess_exec", fake_exec)

    lines: list[str] = []
    async for line in engine.stream_logs("sbx-1"):
        lines.append(line)

    assert calls == [["podman", "logs", "--follow", "sbx-1"]]
    assert lines == ["hello", "world"]


@pytest.mark.asyncio
async def test_podman_stream_logs_terminates_on_consumer_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = PodmanEngine(runtime="docker")
    procs: list[_FakeProc] = []

    async def fake_exec(*argv: str, **kwargs: Any) -> _FakeProc:
        p = _FakeProc([b"only\n", b"more\n"])
        procs.append(p)
        return p

    monkeypatch.setattr("girder.sandbox.podman.asyncio.create_subprocess_exec", fake_exec)

    gen = engine.stream_logs("sbx-2")
    first = await gen.__anext__()
    assert first == "only"
    assert procs[0].returncode is None  # still streaming
    await gen.aclose()  # consumer breaks out
    assert procs[0].returncode is not None  # subprocess was terminated


@pytest.mark.asyncio
async def test_local_stream_logs_is_empty(tmp_path: Path) -> None:
    sandbox = LocalExecSandbox(worktree_hint=tmp_path)
    spec = ContainerSpec(name="sbx-3", image="ignored", worktree=tmp_path)
    await sandbox.start(spec)
    collected = [line async for line in sandbox.stream_logs("sbx-3")]
    assert collected == []
