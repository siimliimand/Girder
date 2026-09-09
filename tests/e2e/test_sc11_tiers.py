"""SC-11: autonomy tiers gate the merge over the FULL pipeline.

T0 parks at merge_pending_human and merges after the human click; T1 merges
in one run_to_completion; T2 needs an earned clean_merge_streak.
"""

from __future__ import annotations

import json

import pytest

from girder.db import repo
from girder.db.models import RunStatus
from girder.orchestrator.run_engine import STOP_DESCRIPTORS
from tests.e2e.conftest import (
    FakeGateway,
    MakeDeliveryCtx,
    resp,
    write_commit_of,
)
from tests.e2e.test_sc01_local_green import (
    CALC_WITH_DIVIDE,
    DIVIDE_TEST,
    PROPOSAL,
    README_DIVIDE,
)

pytestmark = pytest.mark.e2e

GOOD_VERDICT = json.dumps(
    {
        "requirements_complete": True,
        "undeclared_changes": [],
        "severity": "none",
        "summary": "ok",
    }
)


def _gateway() -> FakeGateway:
    return FakeGateway(
        responses=[
            *write_commit_of("calculator.py", CALC_WITH_DIVIDE, tc_id="1"),
            *write_commit_of("tests/test_divide.py", DIVIDE_TEST, tc_id="2"),
            *write_commit_of("README.md", README_DIVIDE, tc_id="3"),
            resp(content=GOOD_VERDICT),
        ]
    )


async def _pump_to(engine: object, run_id: str, *stops: str, max_pumps: int = 500) -> str:
    """Pump until one of *stops* (or any STOP_DESCRIPTORS) is returned."""
    allowed = set(stops) | STOP_DESCRIPTORS
    descriptor = ""
    for _ in range(max_pumps):
        descriptor = await engine.pump_once(run_id)  # type: ignore[attr-defined]
        if descriptor in allowed:
            return descriptor
    raise AssertionError(f"pump did not converge (last: {descriptor!r})")


async def test_t0_parks_for_human_then_merges_on_click(
    make_delivery_ctx: MakeDeliveryCtx,
) -> None:
    ctx = await make_delivery_ctx(PROPOSAL, autonomy_tier=0)
    engine = ctx.delivery_engine(_gateway())
    descriptor = await engine.run_to_completion(ctx.run.id)
    assert descriptor == "merge_pending_human"

    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None and fresh.status is RunStatus.MERGE_PENDING_HUMAN
    assert ctx.api.merge_calls == 0  # T0 never merges by itself
    assert any("ready to merge" in c[1] for c in ctx.notifier.calls)

    # human clicks merge out-of-band; _pump_merge_pending calls no model
    ctx.api.pr_merged = True
    descriptor = await _pump_to(engine, ctx.run.id, "merged")
    assert descriptor == "merged"
    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None and fresh.status is RunStatus.MERGED
    project = await repo.get_project(ctx.db, ctx.project.id)
    assert project is not None and project.clean_merge_streak == 1


async def test_t1_auto_merges_in_one_run_to_completion(
    make_delivery_ctx: MakeDeliveryCtx,
) -> None:
    ctx = await make_delivery_ctx(PROPOSAL, autonomy_tier=1)
    descriptor = await ctx.delivery_engine(_gateway()).run_to_completion(ctx.run.id)
    assert descriptor == "merged"
    assert ctx.api.merge_calls == 1
    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None and fresh.status is RunStatus.MERGED
    project = await repo.get_project(ctx.db, ctx.project.id)
    assert project is not None and project.clean_merge_streak == 1
    assert any("merged" in c[1].lower() for c in ctx.notifier.calls)


async def test_t2_without_earned_streak_is_gated(make_delivery_ctx: MakeDeliveryCtx) -> None:
    ctx = await make_delivery_ctx(PROPOSAL, autonomy_tier=2, clean_merge_streak=0)
    descriptor = await ctx.delivery_engine(_gateway()).run_to_completion(ctx.run.id)
    assert descriptor == "merge_pending_human"
    assert ctx.api.merge_calls == 0

    # a fresh project that HAS earned the streak (>= t2_required_streak) merges
    ctx2 = await make_delivery_ctx(PROPOSAL, autonomy_tier=2, clean_merge_streak=10)
    descriptor = await ctx2.delivery_engine(_gateway()).run_to_completion(ctx2.run.id)
    assert descriptor == "merged"
    assert ctx2.api.merge_calls == 1
    fresh = await repo.get_run(ctx2.db, ctx2.run.id)
    assert fresh is not None and fresh.status is RunStatus.MERGED
