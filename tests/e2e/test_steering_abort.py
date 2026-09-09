"""Abort steering teardown (plan.md Phase 5 task 2, impl-plan §6.12).

An abort must act IMMEDIATELY, not at the next pump boundary: live containers
are killed, in-flight attempt tasks cancelled (attempt → ``crashed``), the
run's worktrees pruned and the run branch reset to ``main`` — plus the
baseline anchoring rule (Phase 2 task 1: baseline sits on ``main``, never on
wherever the checkout's HEAD happens to be).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from girder.db import repo
from girder.db.models import AttemptStatus, RunStatus, SteeringKind
from tests.e2e.conftest import FakeGateway, MakeCtx, write_commit_of

pytestmark = pytest.mark.e2e

CALC_WITH_DIVIDE = (
    '"""Calculator with division."""\n\n\n'
    "class Calculator:\n"
    "    def add(self, a: int, b: int) -> int:\n"
    '        """Return the sum."""\n'
    "        return a + b\n"
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


async def test_abort_mid_attempt_kills_and_resets(make_ctx: MakeCtx) -> None:
    """Abort observed while an attempt is live: the container is killed
    immediately, the attempt goes to CRASHED, worktrees are pruned, the run
    branch sits back at main, the run is ABORTED and a notification goes out."""
    ctx = await make_ctx(PROPOSAL)
    gateway = FakeGateway(
        responses=[],
        routes={
            "Implement divide": write_commit_of("calculator.py", CALC_WITH_DIVIDE)
        },
        delays={"Implement divide": 1.5},
    )
    engine = ctx.engine(gateway)

    assert await engine.pump_once(ctx.run.id) == "active"  # baseline
    pump_task = asyncio.create_task(engine.pump_once(ctx.run.id))
    for _ in range(200):
        if gateway.calls:
            break
        await asyncio.sleep(0.02)
    assert gateway.calls, "attempt never started"

    await repo.insert_steering_event(ctx.db, ctx.run.id, SteeringKind.ABORT.value, {})
    assert await pump_task == "aborted"

    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None and fresh.status is RunStatus.ABORTED

    # container killed, not left running to natural completion
    assert engine.sandbox.killed, "no container was killed by the abort"

    # attempt → crashed (the cancellation protocol: no other status writes)
    attempts = await repo.list_attempts_for_task(
        ctx.db, (await repo.list_tasks_for_run(ctx.db, ctx.run.id))[0].id
    )
    assert attempts
    assert all(a.status == AttemptStatus.CRASHED for a in attempts)

    # no active worktrees left, branch reset to main
    active = await repo.list_worktrees(ctx.db, state=None)
    assert all(w.state.value != "active" for w in active)
    main_tip = await ctx.engine(gateway)._base_commit(ctx.repo_path)
    ops_tip = await _branch_tip(ctx.repo_path, fresh.branch)
    assert ops_tip == main_tip

    assert ctx.notifier.calls, "abort must notify"
    assert any("abort" in body.lower() for _, _, body in ctx.notifier.calls)


async def test_abort_while_idle_resets_branch(make_ctx: MakeCtx) -> None:
    """Abort with no live attempt still performs the branch reset and notify."""
    ctx = await make_ctx(PROPOSAL)
    gateway = FakeGateway(responses=write_commit_of("calculator.py", CALC_WITH_DIVIDE))
    engine = ctx.engine(gateway)

    assert await engine.pump_once(ctx.run.id) == "active"

    await repo.insert_steering_event(ctx.db, ctx.run.id, SteeringKind.ABORT.value, {})
    assert await engine.pump_once(ctx.run.id) == "aborted"

    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None and fresh.status is RunStatus.ABORTED
    main_tip = await engine._base_commit(ctx.repo_path)
    assert await _branch_tip(ctx.repo_path, fresh.branch) == main_tip
    assert any("abort" in body.lower() for _, _, body in ctx.notifier.calls)


async def test_baseline_anchors_at_main_not_checkout_head(make_ctx: MakeCtx) -> None:
    """Phase 2 task 1: the baseline commit resolves from ``main`` even when
    the project checkout sits on another branch."""
    ctx = await make_ctx(PROPOSAL)
    from tests.e2e.conftest import git

    await git(ctx.repo_path, "checkout", "-q", "-b", "operator-experiment")
    (ctx.repo_path / "scratch.py").write_text("x = 1\n")
    await git(ctx.repo_path, "add", "-A")
    await git(ctx.repo_path, "commit", "-m", "unrelated side-branch work")
    head = await git(ctx.repo_path, "rev-parse", "HEAD")
    main_tip = await git(ctx.repo_path, "rev-parse", "main")
    assert head != main_tip

    engine = ctx.engine(FakeGateway(responses=[]))
    assert await engine._base_commit(ctx.repo_path) == main_tip.strip()


async def _branch_tip(repo_path: Any, branch: str) -> str:
    from girder.gitops.branch import BranchOps

    tip = await BranchOps(repo_path).run_branch_tip(branch)
    assert tip is not None
    return tip
