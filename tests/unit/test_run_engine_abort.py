"""Unit tests for the abort watcher loop in RunEngine (Group F fix pass).

The watcher must stay alive for the whole wave: an abort queued after the
first task settles must still cancel the remaining tasks, not run them
unprotected."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from girder.orchestrator import run_engine as run_engine_mod
from girder.orchestrator.run_engine import RunEngine


class _StubEngine:
    """Just enough of RunEngine for _run_attempt_with_abort_watch."""

    def __init__(self, watcher_settle_s: float) -> None:
        self._inflight: dict[str, Any] = {}
        self._watcher_settle_s = watcher_settle_s
        self.abort_fired = False

    async def _watch_abort(self, run_id: str) -> None:
        await asyncio.sleep(self._watcher_settle_s)

    async def _abort_run(
        self,
        run: Any,
        repo_path: Path | None,
        tasks: list[asyncio.Task[Any]] | None = None,
    ) -> str:
        self.abort_fired = True
        if tasks is None:
            tasks = []
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return "aborted"


async def test_abort_after_first_task_settles_still_cancels_rest(monkeypatch) -> None:
    """Red on the old one-shot asyncio.wait: the long coroutine ran to
    completion because the watcher was cancelled after the fast task won."""
    monkeypatch.setattr(run_engine_mod, "_ABORT_POLL_S", 0.01)
    long_done_naturally = False

    async def fast_task() -> Any:
        await asyncio.sleep(0.01)
        return "fast"

    async def long_task() -> Any:
        nonlocal long_done_naturally
        try:
            await asyncio.sleep(5)
            long_done_naturally = True
        except asyncio.CancelledError:
            raise
        return "long"

    stub = _StubEngine(watcher_settle_s=0.3)
    result = await RunEngine._run_attempt_with_abort_watch(
        stub,  # type: ignore[arg-type]
        SimpleNamespace(id="run-1"),  # type: ignore[arg-type]  # run
        Path("."),  # repo_path
        fast_task(),
        long_task(),
    )

    assert result is None, f"expected abort, got results: {result}"
    assert stub.abort_fired, "abort teardown never ran"
    assert not long_done_naturally, "long task ran to completion unprotected"
