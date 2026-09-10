"""CLI behaviours: concurrent pump scheduling (issue 23), local-sandbox guard
(issue 27), the ``girder review`` command (plan.md §2.3 review window), and
the ``girder web`` bind modes (impl-plan §10: TCP loopback or UDS)."""

from __future__ import annotations

import asyncio
from argparse import Namespace
from pathlib import Path
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


# ------------------------------------------------------------------ web (UDS)


async def test_web_forwards_unix_socket_to_uvicorn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """impl-plan §10 "or UDS": a configured unix_socket reaches uvicorn's
    ``uds=`` argument instead of host/port; a stale socket file is unlinked."""
    import uvicorn as uvicorn_module

    from girder.cli import cmd_web
    from girder.config import Settings

    captured: dict[str, object] = {}

    class FakeConfig:
        def __init__(self, app: object, **kwargs: object) -> None:
            captured.update(kwargs)

        @property
        def kwargs(self) -> dict[str, object]:  # pragma: no cover - not used
            return dict(captured)

    class FakeServer:
        def __init__(self, config: FakeConfig) -> None:
            pass

        async def serve(self) -> None:
            return None

    monkeypatch.setattr(uvicorn_module, "Config", FakeConfig)
    monkeypatch.setattr(uvicorn_module, "Server", FakeServer)
    monkeypatch.setattr(
        "girder.cli.load_settings",
        lambda: Settings.model_validate({"web": {"unix_socket": str(tmp_path / "g.sock")}}),
    )
    monkeypatch.setattr("girder.cli.create_app", lambda **_: object())

    stale = tmp_path / "g.sock"
    stale.write_bytes(b"")  # simulate a leftover socket from a crash
    rc = await cmd_web(
        Namespace(db=str(tmp_path / "db.sqlite"), migrations_dir=None,
                  host=None, port=None, unix_socket=None)
    )

    assert rc == 0
    assert captured.get("uds") == str(stale)
    assert "host" not in captured and "port" not in captured
    assert not stale.exists(), "stale socket must be unlinked before binding"


async def test_web_without_unix_socket_binds_host_and_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import uvicorn as uvicorn_module

    from girder.cli import cmd_web
    from girder.config import Settings

    captured: dict[str, object] = {}

    class FakeConfig:
        def __init__(self, app: object, **kwargs: object) -> None:
            captured.update(kwargs)

    class FakeServer:
        def __init__(self, config: FakeConfig) -> None:
            pass

        async def serve(self) -> None:
            return None

    monkeypatch.setattr(uvicorn_module, "Config", FakeConfig)
    monkeypatch.setattr(uvicorn_module, "Server", FakeServer)
    monkeypatch.setattr("girder.cli.load_settings", lambda: Settings())
    monkeypatch.setattr("girder.cli.create_app", lambda **_: object())

    rc = await cmd_web(
        Namespace(db=str(tmp_path / "db.sqlite"), migrations_dir=None,
                  host=None, port=None, unix_socket=None)
    )
    assert rc == 0
    assert captured.get("host") == "127.0.0.1" and captured.get("port") == 8787
    assert "uds" not in captured


# ------------------------------------------------------------- daemon startup


def _roles_settings() -> object:
    from girder.config import ModelsConfig, Settings

    return Settings(
        models=ModelsConfig(
            roles=[
                {"role": "tier1", "provider": "stub", "model": "a"},
                {"role": "tier2", "provider": "stub", "model": "b"},
                {"role": "tier3", "provider": "stub", "model": "c"},
            ]
        )
    )


async def test_daemon_refuses_to_start_without_model_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §6.1: at least one model role per tier — config validation only
    warns, but the daemon must not start pumping with an empty registry."""
    from girder.cli import cmd_daemon
    from girder.config import Secrets, Settings

    monkeypatch.setattr("girder.cli.load_settings", lambda: Settings())
    monkeypatch.setattr("girder.cli.load_secrets", lambda: Secrets())
    rc = await cmd_daemon(
        Namespace(db=":memory:", migrations_dir=None, sandbox="podman")
    )
    assert rc == 2


async def test_daemon_guard_is_overridable_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """GIRDER_ALLOW_NO_ROLES=1 opts out (test/dev harnesses)."""
    import girder.cli as cli_module
    from girder.cli import cmd_daemon
    from girder.config import Secrets, Settings

    monkeypatch.setattr("girder.cli.load_settings", lambda: Settings())
    monkeypatch.setattr("girder.cli.load_secrets", lambda: Secrets())
    monkeypatch.setenv("GIRDER_ALLOW_NO_ROLES", "1")

    async def refuse_to_proceed(args: object) -> object:
        raise RuntimeError("guard passed — reached db open")

    monkeypatch.setattr(cli_module, "_open_db", refuse_to_proceed)
    with pytest.raises(RuntimeError, match="guard passed"):
        await cmd_daemon(Namespace(db=":memory:", migrations_dir=None, sandbox="podman"))


async def test_daemon_starts_with_roles_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The empty-registry guard does not fire when roles exist."""
    import girder.cli as cli_module
    from girder.cli import cmd_daemon
    from girder.config import Secrets

    monkeypatch.setattr("girder.cli.load_settings", _roles_settings)
    monkeypatch.setattr("girder.cli.load_secrets", lambda: Secrets())

    async def refuse_to_proceed(args: object) -> object:
        raise RuntimeError("guard passed — reached db open")

    monkeypatch.setattr(cli_module, "_open_db", refuse_to_proceed)
    with pytest.raises(RuntimeError, match="guard passed"):
        await cmd_daemon(Namespace(db=":memory:", migrations_dir=None, sandbox="podman"))


# ------------------------------------------------------ bounded shutdown drain


async def test_drain_cancels_background_tasks() -> None:
    """Regression (real incident): the daemon's shutdown gathered the GC task
    without ever cancelling it — run_forever() is an infinite loop, so every
    graceful Ctrl+C parked the daemon permanently. _drain must cancel every
    task it is given and return once they are done."""
    from girder.cli import _drain

    async def run_forever() -> None:
        while True:  # noqa: ASYNC110 — deliberately immortal fixture
            await asyncio.sleep(3600)

    immortal = asyncio.create_task(run_forever(), name="gc")
    done = asyncio.create_task(asyncio.sleep(0), name="pump")
    # Bounded outer guard: if _drain ever hangs again, fail instead of hanging.
    await asyncio.wait_for(_drain([immortal, done]), timeout=5)
    assert immortal.cancelled()
    assert done.done()


async def test_drain_abandons_wedged_task_after_timeout() -> None:
    """A task whose cleanup refuses cancellation must not hang shutdown:
    _drain logs and returns after the timeout (second Ctrl+C then force-exits)."""
    from girder.cli import _drain

    refusals = 100  # outlast however many cancels the timeout machinery fans out

    async def wedged() -> None:
        nonlocal refusals
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                if refusals <= 0:
                    raise
                refusals -= 1  # swallow a few cancels, then die

    task = asyncio.create_task(wedged(), name="wedged")
    await asyncio.sleep(0)  # let it start and park inside its sleep
    # Must RETURN (not raise, not hang) despite the task ignoring cancels.
    await asyncio.wait_for(_drain([task], timeout=0.2), timeout=5)
    assert not task.done()
    # Cleanup: keep cancelling until it actually dies (must not leak into the
    # loop teardown, where an un-cancellable task would hang the runner).
    for _ in range(300):
        if task.done():
            break
        task.cancel()
        try:
            # shield: wait_for's own timeout-cancel must not consume one of
            # the task's refusals; each iteration delivers exactly one cancel.
            await asyncio.wait_for(asyncio.shield(task), 0.05)
        except (TimeoutError, asyncio.CancelledError):
            pass
    assert task.done()
