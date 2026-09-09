"""CLI behaviours: concurrent pump scheduling (issue 23), local-sandbox guard
(issue 27), and the ``girder review`` command (plan.md §2.3 review window)."""

from __future__ import annotations

import asyncio
from argparse import Namespace
from types import SimpleNamespace

import pytest

from girder.cli import (
    LocalSandboxRefused,
    _build_sandbox,
    _due_run_ids,
    pump_runs_concurrently,
    review_run,
)
from girder.db import repo
from tests.conftest import seed_run_status

# ------------------------------------------------- concurrent pump scheduling


async def test_blocked_run_does_not_starve_others() -> None:
    """Run A's pump blocks; run B's pump still executes (issue 23)."""
    release = asyncio.Event()
    ran: list[str] = []

    async def pump(run_id: str) -> str:
        if run_id == "A":
            await release.wait()
        ran.append(run_id)
        return f"done {run_id}"

    active: dict[str, asyncio.Task[None]] = {}
    await pump_runs_concurrently(["A", "B"], pump, active)
    await asyncio.sleep(0)  # let the spawned tasks start
    # both pumps were spawned even though A never returned
    assert sorted(ran) == ["B"]
    await pump_runs_concurrently([], pump, active)  # next cycle reaps finished B
    assert set(active) == {"A"}  # A still in flight, B reaped
    release.set()
    await asyncio.gather(*active.values())
    assert sorted(ran) == ["A", "B"]


async def test_crashing_pump_does_not_kill_the_cycle() -> None:
    async def pump(run_id: str) -> str:
        if run_id == "A":
            raise RuntimeError("boom")
        return f"done {run_id}"

    active: dict[str, asyncio.Task[None]] = {}
    await pump_runs_concurrently(["A", "B"], pump, active)
    await asyncio.gather(*active.values())  # no exception escapes
    assert all(task.done() and task.exception() is None for task in active.values())


async def test_due_run_ids_skips_in_flight_runs() -> None:
    task = asyncio.get_running_loop().create_task(asyncio.sleep(0))
    active = {"A": task}
    assert _due_run_ids(["A", "B"], active) == ["B"]
    assert _due_run_ids(["A"], {"A": task}) == []


# --------------------------------------------------------- local sandbox guard


def _args(**kw: object) -> Namespace:
    return Namespace(sandbox="local", **kw)


def test_local_sandbox_refused_without_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIRDER_DEV", raising=False)
    with pytest.raises(LocalSandboxRefused, match="NOT A SECURITY BOUNDARY"):
        _build_sandbox(_args(dev=False), settings=None)  # type: ignore[arg-type]


def test_local_sandbox_allowed_with_dev_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    monkeypatch.delenv("GIRDER_DEV", raising=False)
    sandbox = _build_sandbox(_args(dev=True), settings=SimpleNamespace(sandbox=object()))  # type: ignore[arg-type]
    assert type(sandbox).__name__ == "LocalExecSandbox"


def test_local_sandbox_allowed_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIRDER_DEV", "1")
    sandbox = _build_sandbox(_args(dev=False), settings=SimpleNamespace(sandbox=object()))  # type: ignore[arg-type]
    assert type(sandbox).__name__ == "LocalExecSandbox"


# ------------------------------------------------------------- review command


async def test_review_inserts_merge_reviewed_event(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/tmp/somewhere")
    run = await repo.create_run(db, project.id, "intent", "run/r1", 5.0)
    await seed_run_status(db, run.id, "merged")
    rc = await review_run(db, run.id)
    assert rc == 0
    events = await repo.list_events_for_run(db, run.id)
    assert [e["event_type"] for e in events] == ["merge_reviewed"]


async def test_review_refuses_non_merged_run(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/tmp/somewhere")
    run = await repo.create_run(db, project.id, "intent", "run/r2", 5.0)
    await seed_run_status(db, run.id, "active")
    assert await review_run(db, run.id) == 1
    assert await repo.list_events_for_run(db, run.id) == []


async def test_review_refuses_unknown_run(db) -> None:  # type: ignore[no-untyped-def]
    assert await review_run(db, "no-such-run") == 1
