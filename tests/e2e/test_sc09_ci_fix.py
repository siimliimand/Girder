"""SC-09: a novel (lint-shaped) CI failure is diagnosed and fixed, then merged.

The failing check's output contains NO pytest nodeids ⇒ the flaky/baseline
classifier cannot run ⇒ straight to the Diagnostic Fix Agent, whose scripted
compliant fix keeps the REAL fixture suite green. Drives pump_once manually
so the test can flip the fake checks to green once the fix has been pushed.
"""

from __future__ import annotations

import json

import pytest

from girder.db import repo
from girder.db.models import RunStatus, TaskType
from girder.orchestrator.run_engine import STOP_DESCRIPTORS
from tests.e2e.conftest import (
    FakeGateway,
    MakeDeliveryCtx,
    gh_check,
    resp,
    tc,
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


async def test_sc09_lint_failure_fixed_then_merged(make_delivery_ctx: MakeDeliveryCtx) -> None:
    ctx = await make_delivery_ctx(PROPOSAL, autonomy_tier=1)
    # lint-shaped failure: no nodeids anywhere ⇒ novel, not flaky-classifiable
    ctx.api.checks = [
        gh_check(
            "lint",
            "failure",
            summary="F401 unused import — ruff failed",
            text="src/calculator.py:1: F401 unreferenced",
        )
    ]
    gateway = FakeGateway(
        responses=[
            *write_commit_of("calculator.py", CALC_WITH_DIVIDE, tc_id="1"),
            *write_commit_of("tests/test_divide.py", DIVIDE_TEST, tc_id="2"),
            *write_commit_of("README.md", README_DIVIDE, tc_id="3"),
            # diagnostic fix agent: a compliant fix that keeps the suite green
            resp(calls=[tc("f1", "write_file",
                           json.dumps({"path": "calculator.py",
                                       "content": CALC_WITH_DIVIDE + "# fixed lint\n"}))]),
            resp(calls=[tc("f2", "run_command",
                           '{"cmd":"git add -A && git commit -m fix-lint"}')]),
            resp(calls=[tc("f3", "mark_task_complete", '{"summary":"fixed"}')]),
            resp(content=GOOD_VERDICT),
        ]
    )
    engine = ctx.delivery_engine(gateway)

    flipped = False
    descriptor = ""
    for _ in range(500):
        descriptor = await engine.pump_once(ctx.run.id)
        if descriptor == "ci_red_fixing" and not flipped:
            # the fix agent has run by the *next* pump; the re-poll must see green
            ctx.api.checks = [gh_check("lint", "success")]
            flipped = True
        if descriptor in STOP_DESCRIPTORS:
            break
    assert descriptor == "merged"

    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None and fresh.status is RunStatus.MERGED

    tasks = await repo.list_tasks_for_run(ctx.db, ctx.run.id)
    fix_tasks = [t for t in tasks if t.title.startswith("fix:")]
    assert len(fix_tasks) == 1
    assert fix_tasks[0].task_type is TaskType.FIX

    ci_rows = await repo.list_ci_check_results_for_run(ctx.db, ctx.run.id)
    assert ci_rows  # the red report was persisted (redacted) for the audit trail

    events = await ctx.db.fetchall(
        "SELECT event_type FROM agent_events WHERE run_id = ?", (ctx.run.id,)
    )
    assert any(e["event_type"] == "ci_fix_attempted" for e in events)

    assert any("merged" in c[1].lower() for c in ctx.notifier.calls)
    project = await repo.get_project(ctx.db, ctx.project.id)
    assert project is not None and project.clean_merge_streak == 1
