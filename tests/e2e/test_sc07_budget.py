"""SC-07: a near-zero budget cap stops the run before ANY model dispatch
(Phase 2 exit criterion 4): real ModelGateway over httpx.MockTransport,
zero HTTP calls, attempt budget_frozen, run budget_exhausted."""

from __future__ import annotations

import httpx
import pytest

from girder.budget.guard import BudgetGuard
from girder.config import ModelRole, ModelsConfig, Secrets
from girder.db import repo
from girder.db.models import AttemptStatus, RunStatus
from girder.models.gateway import ModelGateway
from tests.e2e.conftest import MakeCtx

pytestmark = pytest.mark.e2e

PROPOSAL = """\
---
schema: girder.openspec/v1
title: Implement divide
intent: The calculator can divide.
tasks:
  - id: implement-divide
    title: Implement divide
    type: code_change
    scope_globs: ["calculator.py"]
    success_criteria: ["divide works"]
    depends_on: []
---
Narrative.
"""


class NoDispatchTransport(httpx.MockTransport):
    def __init__(self) -> None:
        super().__init__(self._handle)
        self.calls = 0

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        raise AssertionError("no HTTP may be dispatched under a zero-ish cap")


async def test_sc07_exhausted_budget_blocks_all_dispatches(make_ctx: MakeCtx) -> None:
    ctx = await make_ctx(PROPOSAL, budget_cap_usd=0.000001)

    settings = ctx.settings.model_copy(
        update={
            "models": ModelsConfig(
                roles=[
                    ModelRole(
                        role=role,
                        provider="openrouter",
                        model="m",
                        price_in_per_mtok=30.0,
                        price_out_per_mtok=60.0,
                    )
                    for role in ("tier1", "tier2", "tier3")
                ]
            )
        }
    )
    transport = NoDispatchTransport()
    gateway = ModelGateway(
        settings,
        Secrets(models_openrouter_api_key="sk-test-nonsecret"),
        ctx.db,
        ctx.redactor,
        BudgetGuard(ctx.db),
        None,
        transport=transport,
    )

    descriptor = await ctx.engine(gateway).run_to_completion(ctx.run.id)
    assert descriptor == "budget_exhausted"

    assert transport.calls == 0  # the deny aborted the dispatch, not the reverse

    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None and fresh.status is RunStatus.BUDGET_EXHAUSTED

    tasks = await repo.list_tasks_for_run(ctx.db, ctx.run.id)
    assert len(tasks) == 1
    attempts = await ctx.db.fetchall(
        "SELECT status FROM attempts WHERE task_id = ?", (tasks[0].id,)
    )
    assert [a["status"] for a in attempts] == [AttemptStatus.BUDGET_FROZEN.value]

    # the pre-flight denial was recorded and the task never ran verify
    events = {
        e["event_type"] for e in await ctx.db.fetchall(
            "SELECT event_type FROM agent_events WHERE run_id = ?", (ctx.run.id,)
        )
    }
    assert "budget_preflight_denied" in events
    assert "task_completed" not in events


async def test_sc07_post_call_tripwire_freezes_and_notifies(make_ctx: MakeCtx) -> None:
    """Sprint 6 SC-07 extension: the cap tripwire also fires on the *actual*
    path — a call that passed pre-flight but whose reconciled cost lands over
    the cap halts the run (budget_exhausted) and notifies immediately."""
    ctx = await make_ctx(PROPOSAL, budget_cap_usd=0.005)

    settings = ctx.settings.model_copy(
        update={
            "models": ModelsConfig(
                roles=[
                    ModelRole(
                        role=role,
                        provider="openrouter",
                        model="m",
                        price_in_per_mtok=0.15,
                        price_out_per_mtok=0.6,
                    )
                    for role in ("tier1", "tier2", "tier3")
                ]
            )
        }
    )

    class TripwireTransport(httpx.MockTransport):
        def __init__(self) -> None:
            super().__init__(self._handle)
            self.calls = 0

        def _handle(self, request: httpx.Request) -> httpx.Response:
            self.calls += 1
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "t1",
                                        "function": {
                                            "name": "mark_task_complete",
                                            "arguments": '{"summary": "done"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    # actual cost: 100*0.15/1e6 + 20000*0.6/1e6 = $0.012 > cap
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20000},
                },
            )

    transport = TripwireTransport()
    gateway = ModelGateway(
        settings,
        Secrets(models_openrouter_api_key="sk-test-nonsecret"),
        ctx.db,
        ctx.redactor,
        BudgetGuard(ctx.db),
        ctx.notifier,  # the tripwire must reach the notification engine
        transport=transport,
    )

    # Pre-flight estimate (~4096 * 0.6/1e6 ≈ $0.0025) fits the $0.005 cap, so
    # the call IS dispatched; the reconciled actual overshoots and trips.
    descriptor = await ctx.engine(gateway).run_to_completion(ctx.run.id)
    assert descriptor == "budget_exhausted"

    assert transport.calls == 1  # exactly one real dispatch happened

    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None and fresh.status is RunStatus.BUDGET_EXHAUSTED
    assert fresh.spend_usd > fresh.budget_cap_usd

    events = {
        e["event_type"] for e in await ctx.db.fetchall(
            "SELECT event_type FROM agent_events WHERE run_id = ?", (ctx.run.id,)
        )
    }
    assert "budget_tripwire" in events
    assert "model_call_completed" in events  # the paid-for response was still used

    notified = [title for _level, title, _body in ctx.notifier.calls]
    assert any("post-call tripwire" in t for t in notified)
