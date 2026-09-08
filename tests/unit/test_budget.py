"""WP-2.1 — budget guard: pure pre-flight math + DB reconciliation."""

from __future__ import annotations

import pytest

from girder.budget.guard import BudgetGuard
from girder.config import ModelRole
from girder.db import repo
from girder.db.models import Run, RunStatus


def _role(**overrides: object) -> ModelRole:
    defaults: dict = {
        "role": "tier1",
        "provider": "openrouter",
        "model": "test/cheap-model",
        "max_output_tokens": 1000,
        "price_in_per_mtok": 3.0,
        "price_out_per_mtok": 15.0,
    }
    defaults.update(overrides)
    return ModelRole(**defaults)  # type: ignore[arg-type]


def _run(**overrides: object) -> Run:
    defaults: dict = {
        "id": "run_x",
        "project_id": "p",
        "intent": "i",
        "branch": "b",
        "status": RunStatus.ACTIVE,
        "budget_cap_usd": 5.0,
        "spend_usd": 0.0,
    }
    defaults.update(overrides)
    return Run(**defaults)  # type: ignore[arg-type]


def test_estimate_formula_exact() -> None:
    # price_in 3.0/MTok, price_out 15.0/MTok, max_output 1000, prompt 8000 chars.
    guard = BudgetGuard(db=None)  # type: ignore[arg-type]  # preflight is pure
    d = guard.preflight(_role(), 8000, _run())
    assert d.est_in_tokens == 2000
    assert d.est_out_tokens == 1000
    assert d.est_cost_usd == pytest.approx(2000 * 3e-6 + 1000 * 15e-6)  # 0.006 + 0.015 = 0.021
    assert d.allowed is True
    assert d.remaining_usd == pytest.approx(5.0)


def test_estimate_ceils_partial_tokens() -> None:
    guard = BudgetGuard(db=None)  # type: ignore[arg-type]
    d = guard.preflight(_role(), 8001, _run())
    assert d.est_in_tokens == 2001


def test_est_out_capped_by_remaining_budget() -> None:
    # remaining 0.01 -> int(0.01 / 0.000015) = 666 output tokens;
    # est_cost = 0.006 + 666*0.000015 = 0.01599 > cap -> denied.
    guard = BudgetGuard(db=None)  # type: ignore[arg-type]
    d = guard.preflight(_role(), 8000, _run(budget_cap_usd=0.01))
    assert d.est_out_tokens == 666
    assert d.est_cost_usd == pytest.approx(0.006 + 666 * 0.000015)
    assert d.allowed is False
    assert d.reason is not None


def test_zero_prices_allows_unbounded_output_estimate() -> None:
    guard = BudgetGuard(db=None)  # type: ignore[arg-type]
    d = guard.preflight(_role(price_in_per_mtok=0.0, price_out_per_mtok=0.0), 8000, _run())
    assert d.est_cost_usd == 0.0
    assert d.est_out_tokens == 1000
    assert d.allowed is True


def test_spend_equal_to_cap_with_zero_cost_is_allowed() -> None:
    # <= semantics: spend == cap and est_cost == 0 is still allowed.
    guard = BudgetGuard(db=None)  # type: ignore[arg-type]
    d = guard.preflight(
        _role(price_in_per_mtok=0.0, price_out_per_mtok=0.0),
        8000,
        _run(budget_cap_usd=1.0, spend_usd=1.0),
    )
    assert d.allowed is True


def test_spend_over_cap_denied() -> None:
    guard = BudgetGuard(db=None)  # type: ignore[arg-type]
    d = guard.preflight(
        _role(price_in_per_mtok=0.0, price_out_per_mtok=0.0),
        8000,
        _run(budget_cap_usd=1.0, spend_usd=1.01),
    )
    assert d.allowed is False
    assert d.remaining_usd == 0.0


async def test_reconcile_writes_actuals_and_accumulates_spend(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/tmp/p")
    run = await repo.create_run(db, project.id, "intent", "branch", budget_cap_usd=5.0)

    guard = BudgetGuard(db)
    usage_row_id = await repo.insert_token_usage(
        db,
        run_id=run.id,
        attempt_id=None,
        model_role="tier1",
        model_id="test/cheap-model",
        estimated_before_call=0.021,
    )
    outcome = await guard.reconcile(
        usage_row_id,
        run.id,
        _role(),
        prompt_tokens=10,
        completion_tokens=5,
        projected_total=0.02,
    )
    assert outcome.actual_cost_usd == pytest.approx(10 * 3e-6 + 5 * 15e-6)  # 0.000105
    assert outcome.run_spend_usd == pytest.approx(outcome.actual_cost_usd)
    assert outcome.over_budget is False

    rows = await repo.list_token_usage_for_run(db, run.id)
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 10
    assert rows[0]["completion_tokens"] == 5
    assert rows[0]["cost_usd"] == pytest.approx(outcome.actual_cost_usd)
    assert rows[0]["estimated_before_call"] == pytest.approx(0.021)

    updated = await repo.get_run(db, run.id)
    assert updated is not None
    assert updated.spend_usd == pytest.approx(outcome.actual_cost_usd)
    # projected_total OVERWRITES projected_spend_usD rather than accumulating.
    assert updated.projected_spend_usd == pytest.approx(0.02)

    # Second reconcile accumulates run.spend_usd.
    await guard.reconcile(
        usage_row_id, run.id, _role(), prompt_tokens=10, completion_tokens=5, projected_total=0.03
    )
    updated = await repo.get_run(db, run.id)
    assert updated is not None
    assert updated.spend_usd == pytest.approx(2 * outcome.actual_cost_usd)


async def test_reconcile_tripwire_fires_over_cap(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/tmp/p")
    run = await repo.create_run(db, project.id, "intent", "branch", budget_cap_usd=0.0001)

    guard = BudgetGuard(db)
    usage_row_id = await repo.insert_token_usage(
        db, run_id=run.id, attempt_id=None, model_role="tier1",
        model_id="test/cheap-model", estimated_before_call=0.0,
    )
    # 1_000_000 out tokens * 15e-6 = 15.00 >> cap 0.0001.
    outcome = await guard.reconcile(
        usage_row_id, run.id, _role(), prompt_tokens=0, completion_tokens=1_000_000,
        projected_total=15.0,
    )
    assert outcome.over_budget is True
    assert outcome.run_spend_usd > run.budget_cap_usd
