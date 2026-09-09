"""SC-16 (Phase 5 exit criterion 1): mid-flight steering.

Pause at a task boundary, inject an operator directive, resume — the run
adapts: the directive reaches the agent tagged ``[TRUSTED]`` (never as
untrusted repo content, D11), and the task still completes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from girder.db import repo
from girder.db.models import RunStatus, SteeringKind, TaskStatus
from tests.e2e.conftest import FakeGateway, MakeCtx, write_commit_of

pytestmark = pytest.mark.e2e

DIRECTIVE = "Keep the implementation minimal — a plain expression is fine."
MID_LOOP_DIRECTIVE = "Add a docstring mentioning the operator review."

CALC_WITH_DIVIDE = (
    '"""Calculator with division."""\n\n\n'
    "class Calculator:\n"
    "    def add(self, a: int, b: int) -> int:\n"
    '        """Return the sum."""\n'
    "        return a + b\n\n"
    "    def subtract(self, a: int, b: int) -> int:\n"
    '        """Return a minus b."""\n'
    "        return a - b\n\n"
    "    def multiply(self, a: int, b: int) -> int:\n"
    '        """Return the product."""\n'
    "        return a * b\n\n"
    "    def divide(self, a: int, b: int) -> float:\n"
    '        """Return a divided by b."""\n'
    "        return a / b\n"
)

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


def _directive_messages(messages: list[Any], needle: str) -> list[str]:
    """User-role messages that carry a trusted steering directive."""
    found: list[str] = []
    for m in messages:
        content = getattr(m, "content", "") or ""
        if getattr(m, "role", "") == "user" and "Steering directive" in content:
            found.append(content)
    return [c for c in found if needle in c]


async def _wait_for(predicate: Callable[[], bool], timeout_s: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_sc16_pause_inject_resume_run_adapts(make_ctx: MakeCtx) -> None:
    """The literal SC-16: pause at the boundary holds the pump without any
    model dispatch; the pending inject is folded into the resumed attempt's
    trusted guidance; the run completes."""
    ctx = await make_ctx(PROPOSAL)
    gateway = FakeGateway(responses=write_commit_of("calculator.py", CALC_WITH_DIVIDE))
    engine = ctx.engine(gateway)

    # Baseline first: steering is observed on the active pump only.
    descriptor = await engine.pump_once(ctx.run.id)
    assert descriptor == "active"

    # 1. Pause at the task boundary: no FSM state, the pump parks.
    await repo.insert_steering_event(ctx.db, ctx.run.id, SteeringKind.PAUSE.value, {})
    descriptor = await engine.pump_once(ctx.run.id)
    assert descriptor == "paused"
    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None and fresh.paused is True
    assert fresh.status is RunStatus.ACTIVE  # pump-level suspend, not a state
    assert gateway.calls == []  # nothing dispatched while paused

    # 2. A paused run stays parked.
    descriptor = await engine.pump_once(ctx.run.id)
    assert descriptor == "paused"
    assert gateway.calls == []

    # 3. Inject the directive (lands while no agent is live), then resume.
    await repo.insert_steering_event(
        ctx.db, ctx.run.id, SteeringKind.INJECT.value, {"directive": DIRECTIVE}
    )
    await repo.insert_steering_event(ctx.db, ctx.run.id, SteeringKind.RESUME.value, {})

    # 4. The run adapts: first attempt prompt carries the [TRUSTED] directive…
    descriptor = await engine.run_to_completion(ctx.run.id)
    assert descriptor == "local_green"
    assert gateway.calls, "agent must have run after resume"
    first_user = next(
        (m for m in gateway.calls[0] if getattr(m, "role", "") == "user"), None
    )
    assert first_user is not None
    assert "[TRUSTED] Steering directive (user-authored):" in first_user.content
    assert DIRECTIVE in first_user.content

    # …and the pipeline finished clean.
    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None and fresh.paused is False
    tasks = await repo.list_tasks_for_run(ctx.db, ctx.run.id)
    assert [t.status for t in tasks] == [TaskStatus.COMPLETED]
    assert await repo.get_latest_event(ctx.db, ctx.run.id, "steering_pause") is not None
    assert await repo.get_latest_event(ctx.db, ctx.run.id, "steering_resume") is not None


async def test_sc16_inject_reaches_live_agent_between_turns(make_ctx: MakeCtx) -> None:
    """An inject landed while an attempt is mid-flight is absorbed at the next
    turn boundary into the conversation as a [TRUSTED] user message."""
    ctx = await make_ctx(PROPOSAL)
    gateway = FakeGateway(
        responses=[],
        routes={
            "Implement divide": write_commit_of("calculator.py", CALC_WITH_DIVIDE, tc_id="m1")
        },
        delays={"Implement divide": 0.2},
    )
    engine = ctx.engine(gateway)

    pump_task = asyncio.create_task(engine.run_to_completion(ctx.run.id))
    assert await _wait_for(lambda: len(gateway.calls) >= 1), "agent never started"

    # The attempt is live (turn 1 answered): inject mid-flight.
    await repo.insert_steering_event(
        ctx.db, ctx.run.id, SteeringKind.INJECT.value, {"directive": MID_LOOP_DIRECTIVE}
    )
    descriptor = await pump_task
    assert descriptor == "local_green"

    assert len(gateway.calls) >= 3, "three scripted turns expected"
    second_call = gateway.calls[1]
    matches = _directive_messages(second_call, MID_LOOP_DIRECTIVE)
    assert matches, f"directive missing from turn-2 context: {second_call!r}"
    assert "[TRUSTED] Steering directive (user-authored):" in matches[0]

    injected = await repo.get_latest_event(ctx.db, ctx.run.id, "steering_injected")
    assert injected is not None
    assert injected["payload"]["directive"] == MID_LOOP_DIRECTIVE

    tasks = await repo.list_tasks_for_run(ctx.db, ctx.run.id)
    assert [t.status for t in tasks] == [TaskStatus.COMPLETED]


async def test_sc16_abort_while_paused_terminates_run(make_ctx: MakeCtx) -> None:
    ctx = await make_ctx(PROPOSAL)
    gateway = FakeGateway(responses=write_commit_of("calculator.py", CALC_WITH_DIVIDE))
    engine = ctx.engine(gateway)

    assert await engine.pump_once(ctx.run.id) == "active"  # baseline

    await repo.insert_steering_event(ctx.db, ctx.run.id, SteeringKind.PAUSE.value, {})
    assert await engine.pump_once(ctx.run.id) == "paused"

    await repo.insert_steering_event(ctx.db, ctx.run.id, SteeringKind.ABORT.value, {})
    assert await engine.pump_once(ctx.run.id) == "aborted"
    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None
    assert fresh.status is RunStatus.ABORTED
    assert fresh.paused is False
    assert gateway.calls == []  # aborted before any dispatch
