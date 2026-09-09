"""SC-14: two independent tasks execute CONCURRENTLY in distinct worktrees
(Sprint 5 Phase 4 exit criterion) and are integrated onto the run branch.

The proposal freezes two disjoint code_change tasks; the wave planner must
run them side by side (task 1 is delayed mid-attempt via FakeGateway.delays
so task 2 provably starts while task 1 is still in flight), then integrate
both into a wave branch and fast-forward run/e2e1. The concurrency assertion
uses FakeGateway.call_times: the FIRST gateway call of task 2 must precede
the LAST gateway call of task 1.
"""

from __future__ import annotations

import pytest

from girder.db import repo
from girder.db.models import RunStatus, TaskStatus
from girder.orchestrator.run_engine import STOP_DESCRIPTORS
from tests.e2e.conftest import FakeGateway, MakeCtx, first_user_content, git, write_commit_of

pytestmark = pytest.mark.e2e

TASK1_TITLE = "Add divide operation"
TASK2_TITLE = "Add stats module"

# Full calculator.py with divide appended — additive, so the REAL baseline +
# verify pytest runs in the worktrees stay green (tests/test_arithmetic.py
# only asserts add/subtract/multiply).
CALC_WITH_DIVIDE = (
    '"""A tiny calculator."""\n\n\n'
    "class Calculator:\n"
    '    """Integer arithmetic on two operands."""\n\n'
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

STATS_MODULE = (
    '"""Summary statistics."""\n\n\n'
    "def mean(values: list[float]) -> float:\n"
    '    """Arithmetic mean."""\n'
    "    return sum(values) / len(values)\n"
)

PROPOSAL = f"""\
---
schema: girder.openspec/v1
title: Extend calculator with divide and a stats module
intent: The calculator divides; a stats module summarizes numbers.
tasks:
  - id: add-divide
    title: {TASK1_TITLE}
    type: code_change
    scope_globs: ["calculator.py"]
    success_criteria: ["divide(6, 3) returns 2.0"]
    depends_on: []
  - id: add-stats
    title: {TASK2_TITLE}
    type: code_change
    scope_globs: ["stats.py"]
    success_criteria: ["mean([1, 2, 3]) returns 2.0"]
    depends_on: []
---
Two independent extensions with disjoint scopes: a wave candidate.
"""


async def test_sc14_two_tasks_execute_concurrently_and_integrate(
    make_ctx: MakeCtx,
) -> None:
    ctx = await make_ctx(PROPOSAL)
    gateway = FakeGateway(
        responses=[],
        routes={
            TASK1_TITLE: write_commit_of("calculator.py", CALC_WITH_DIVIDE, tc_id="t1"),
            TASK2_TITLE: write_commit_of("stats.py", STATS_MODULE, tc_id="t2"),
        },
        # Task 1 stalls mid-attempt so task 2 must start while it is in flight.
        delays={TASK1_TITLE: 1.0},
    )
    engine = ctx.engine(gateway)

    descriptor = ""
    for _ in range(500):
        descriptor = await engine.pump_once(ctx.run.id)
        if descriptor in STOP_DESCRIPTORS:
            break
    assert descriptor == "local_green"

    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None and fresh.status is RunStatus.ACTIVE  # local green pre-delivery

    tasks = await repo.list_tasks_for_run(ctx.db, ctx.run.id)
    by_title = {t.title: t for t in tasks}
    assert set(by_title) == {TASK1_TITLE, TASK2_TITLE}
    for task in by_title.values():
        row = await repo.get_task(ctx.db, task.id)
        assert row is not None and row.status is TaskStatus.COMPLETED

    # Exactly one wave, completed.
    waves = await repo.list_waves_for_run(ctx.db, ctx.run.id)
    assert len(waves) == 1
    assert waves[0].status == "completed"

    # Distinct worktrees, one per task, path layout <base>/<run_id>/<task_id>.
    tasks = await repo.list_tasks_for_run(ctx.db, ctx.run.id)
    task_ids = {t.id for t in tasks}
    wts = [wt for wt in await repo.list_worktrees(ctx.db) if wt.path.split("/")[-1] in task_ids]
    assert len(wts) == 2
    suffixes = {wt.path.rsplit("/", 1)[-1] for wt in wts}
    assert len(suffixes) == 2  # distinct per-task worktree paths

    # CONCURRENCY (Phase 4 exit criterion): bucket every gateway call by its
    # first-user-message routing key; task 2's first call must precede task
    # 1's last call because task 1 was delayed a full second mid-attempt.
    def bucket(messages: list[object]) -> str | None:
        content = first_user_content(messages)  # type: ignore[arg-type]
        for key in gateway.routes:
            if key in content:
                return key
        return None

    times = {TASK1_TITLE: [], TASK2_TITLE: []}
    for messages, ts in zip(gateway.calls, gateway.call_times, strict=True):
        key = bucket(messages)
        if key in times:
            times[key].append(ts)
    assert times[TASK1_TITLE] and times[TASK2_TITLE]
    assert min(times[TASK2_TITLE]) < max(times[TASK1_TITLE])

    # Integration: run/e2e1 carries BOTH changes.
    run_calc = await git(ctx.repo_path, "show", "run/e2e1:calculator.py")
    assert "def divide" in run_calc
    run_stats = await git(ctx.repo_path, "show", "run/e2e1:stats.py")
    assert "def mean" in run_stats
    subjects = (await git(ctx.repo_path, "log", "run/e2e1", "--format=%s")).splitlines()
    assert subjects.count("work") >= 2  # both task commits (or their merges) landed
