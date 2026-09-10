"""WP 10.4 + 10.5 — explicit None-guards and typed event payloads."""

from __future__ import annotations

import pytest

from girder.config import Secrets, Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.payloads import BudgetEventPayload, SteeringPayload, TransitionPayload
from girder.guard.redact import Redactor
from girder.orchestrator.run_engine import RunEngine

from tests.conftest import seed_run_status


async def test_repo_accepts_typed_payloads(db: Database) -> None:
    """Runtime half of TypedDict compatibility: the real payload shapes are
    accepted by the repo functions they flow into (mypy checks the static
    half — TransitionPayload etc. assign cleanly to the Mapping params)."""
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/tp01", 5.0)

    transition: TransitionPayload = {
        "entity": "run",
        "id": run.id,
        "from": "active",
        "to": "parked",
        "reason": "test",
    }
    await repo.insert_event(db, "state_transition", transition, run_id=run.id)

    steering: SteeringPayload = {"directive": "slow down"}
    await repo.insert_steering_event(db, run.id, "inject", steering)
    await repo.insert_event(db, "steering_requested", steering, run_id=run.id)

    budget: BudgetEventPayload = {
        "role": "coder",
        "model": "claude/fake",
        "est_cost_usd": 0.01,
        "remaining_usd": 4.99,
    }
    await repo.insert_event(db, "budget_preflight_denied", budget, run_id=run.id)

    latest = await repo.get_latest_event(db, run.id, "budget_preflight_denied")
    assert latest is not None
    assert latest["payload"] == budget


async def test_get_latest_event_missing_returns_none(db: Database) -> None:
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/tp02", 5.0)
    assert await repo.get_latest_event(db, run.id, "no_such_event") is None


async def test_guard_raises_informative_runtime_error(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WP 10.4: the converted guards are always-on RuntimeErrors with the
    entity id in the message, not -O-strippable asserts. Drives the REAL
    re-fetch-after-transition guard in ``RunEngine._pump_budget_exhausted``
    (run_engine.py) — the test fails if the guard is reverted to a bare
    ``assert`` (or stripped under ``python -O``)."""
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/tp03", 5.0)
    await seed_run_status(db, run.id, "budget_exhausted")

    engine = RunEngine(
        db=db,
        settings=Settings(),
        secrets=Secrets(),
        gateway=None,  # type: ignore[arg-type]
        sandbox=None,  # type: ignore[arg-type]
        notifier=None,
        redactor=Redactor(),
    )

    # Spend < cap so the pump takes the resume path: transition to ACTIVE,
    # then re-fetch the run — which we make vanish (None) to hit the guard.
    # The downstream pump is stubbed so a removed guard fails cleanly with
    # "did not raise" instead of an unrelated AttributeError on None.
    async def vanished(*args: object, **kwargs: object) -> None:
        return None

    async def noop_pump(run: object) -> str:
        return "resumed"

    monkeypatch.setattr(repo, "get_run", vanished)
    monkeypatch.setattr(engine, "_pump_active", noop_pump)
    with pytest.raises(RuntimeError, match=run.id):
        await engine._pump_budget_exhausted(run)
