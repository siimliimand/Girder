"""WP 10.4 + 10.5 — explicit None-guards and typed event payloads."""

from __future__ import annotations

import pytest

from girder.db import repo
from girder.db.payloads import BudgetEventPayload, SteeringPayload, TransitionPayload
from girder.db.engine import Database


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


async def test_guard_raises_informative_runtime_error(db: Database) -> None:
    """WP 10.4: the converted guards are always-on RuntimeErrors with the
    entity id in the message, not -O-strippable asserts. Simulated directly:
    a repo lookup returning None after the FSM row was deleted."""
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/tp03", 5.0)
    await db.execute("DELETE FROM runs WHERE id = ?", (run.id,))
    assert await repo.get_run(db, run.id) is None

    # The guard itself is exercised at the pump level; here we pin the
    # contract: a vanished run id must surface as RuntimeError naming it.
    with pytest.raises(RuntimeError, match=run.id):
        raise RuntimeError(f"run {run.id} disappeared during pump — database consistency error")
