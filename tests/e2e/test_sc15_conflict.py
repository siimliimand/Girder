"""SC-15: a genuine merge conflict at wave integration is either resolved by
the agent-driven resolution loop (full-suite re-test + Tier-1 hunk review) or,
on exhausting ``settings.limits.conflict_resolution_attempts``, the task is
dropped and the run escalates with the wave branch rolled back.

The mechanical planner would separate overlapping-scope tasks into different
waves, so the conflict is induced honestly: the run freezes a one-task
proposal (no decomposition of overlapping scopes) and BOTH tasks are seeded
directly into wave 0 with the same scope glob, each rewriting calculator.py
with conflicting edits to the SAME lines (class docstring + the lines after
``multiply``). Edits are docstring/method additions only, so the REAL pytest
suite stays green in every worktree.
"""

from __future__ import annotations

import json

import pytest

from girder.db import repo
from girder.db.models import RunStatus, Task, TaskStatus, TaskType
from girder.orchestrator.run_engine import STOP_DESCRIPTORS
from tests.e2e.conftest import (
    E2eCtx,
    FakeGateway,
    MakeCtx,
    first_user_content,
    git,
    resp,
    tc,
    write_commit_of,
)

pytestmark = pytest.mark.e2e

TASK_A_TITLE = "Tighten calculator docstring"
TASK_B_TITLE = "Clamp calculator bounds"

_ONE_TASK_PROPOSAL = f"""\
---
schema: girder.openspec/v1
title: Update the calculator class
intent: Small in-place edit to calculator.py.
tasks:
  - id: update-calc
    title: {TASK_A_TITLE}
    type: code_change
    scope_globs: ["calculator.py"]
    success_criteria: ["calculator.py updated in place"]
    depends_on: []
---
Single-task proposal; the second (conflicting) task is seeded directly.
"""

# Both versions rewrite the SAME lines (class docstring AND the tail after
# multiply) with different text — a genuine git conflict at integration —
# while keeping tests/test_arithmetic.py green (add/subtract/multiply intact).
CALC_A = (
    '"""A tiny calculator."""\n\n\n'
    "class Calculator:\n"
    '    """Integer arithmetic on two operands. Docstring tightened."""\n\n'
    "    def add(self, a: int, b: int) -> int:\n"
    '        """Return the sum."""\n'
    "        return a + b\n\n"
    "    def subtract(self, a: int, b: int) -> int:\n"
    '        """Return a minus b."""\n'
    "        return a - b\n\n"
    "    def multiply(self, a: int, b: int) -> int:\n"
    '        """Return the product."""\n'
    "        return a * b\n\n"
    "    def max2(self, a: int, b: int) -> int:\n"
    '        """Return the larger operand."""\n'
    "        return a if a > b else b\n"
)

CALC_B = (
    '"""A tiny calculator."""\n\n\n'
    "class Calculator:\n"
    '    """Integer arithmetic on two operands, bounds-clamped."""\n\n'
    "    def add(self, a: int, b: int) -> int:\n"
    '        """Return the sum."""\n'
    "        return a + b\n\n"
    "    def subtract(self, a: int, b: int) -> int:\n"
    '        """Return a minus b."""\n'
    "        return a - b\n\n"
    "    def multiply(self, a: int, b: int) -> int:\n"
    '        """Return the product."""\n'
    "        return a * b\n\n"
    "    def min2(self, a: int, b: int) -> int:\n"
    '        """Return the smaller operand."""\n'
    "        return a if a < b else b\n"
)

CALC_RESOLVED = (
    '"""A tiny calculator."""\n\n\n'
    "class Calculator:\n"
    '    """Integer arithmetic on two operands; tightened and clamped."""\n\n'
    "    def add(self, a: int, b: int) -> int:\n"
    '        """Return the sum."""\n'
    "        return a + b\n\n"
    "    def subtract(self, a: int, b: int) -> int:\n"
    '        """Return a minus b."""\n'
    "        return a - b\n\n"
    "    def multiply(self, a: int, b: int) -> int:\n"
    '        """Return the product."""\n'
    "        return a * b\n\n"
    "    def max2(self, a: int, b: int) -> int:\n"
    '        """Return the larger operand."""\n'
    "        return a if a > b else b\n\n"
    "    def min2(self, a: int, b: int) -> int:\n"
    '        """Return the smaller operand."""\n'
    "        return a if a < b else b\n"
)

SOUND_VERDICT = json.dumps(
    {"resolution_sound": True, "concerns": [], "severity": "none", "summary": "clean"}
)


async def _seed_conflicting_run(make_ctx: MakeCtx) -> tuple[E2eCtx, tuple[Task, Task]]:
    """Freeze a one-task proposal, then seed BOTH overlapping tasks into
    wave 0 directly — bypassing the wave planner entirely (SC-15 idiom)."""
    ctx = await make_ctx(_ONE_TASK_PROPOSAL)
    wave = await repo.get_or_create_wave0(ctx.db, ctx.run.id)
    task_a = await repo.create_task(
        ctx.db,
        wave.id,
        1,
        TASK_A_TITLE,
        TaskType.CODE_CHANGE,
        scope_globs=["calculator.py"],
        spec_slice_md="## Task\n\nTighten the calculator docstring.\n",
    )
    task_b = await repo.create_task(
        ctx.db,
        wave.id,
        2,
        TASK_B_TITLE,
        TaskType.CODE_CHANGE,
        scope_globs=["calculator.py"],
        spec_slice_md="## Task\n\nClamp the calculator bounds.\n",
    )
    # Mark planning as already done: otherwise the pump's mechanical planner
    # would (correctly!) demote the overlapping task B to its own wave, and
    # the conflict SC-15 exists to exercise could never fire.
    await repo.insert_event(
        ctx.db, "waves_planned", {"waves": [], "demoted_task_ids": []}, run_id=ctx.run.id
    )
    assert ctx.settings.limits.conflict_resolution_attempts == 2
    return ctx, (task_a, task_b)


async def _pump_to_stop(ctx: E2eCtx, gateway: FakeGateway) -> str:
    engine = ctx.engine(gateway)
    descriptor = ""
    for _ in range(500):
        descriptor = await engine.pump_once(ctx.run.id)
        if descriptor in STOP_DESCRIPTORS:
            break
    return descriptor


async def test_sc15_conflict_resolved_then_retested_and_reviewed(make_ctx: MakeCtx) -> None:
    """SC-15 happy path: the conflict resolution agent merges the hunks, the
    full suite is re-run, the Tier-1 'resolution hunk' review passes, and the
    wave branch carries a two-parent merge commit before local green."""
    ctx, (task_a, task_b) = await _seed_conflicting_run(make_ctx)
    gateway = FakeGateway(
        responses=[],
        routes={
            TASK_A_TITLE: write_commit_of("calculator.py", CALC_A, tc_id="a"),
            TASK_B_TITLE: write_commit_of("calculator.py", CALC_B, tc_id="b"),
            # Agent-driven resolution: read the conflicted file, write the
            # merged version, commit, declare done.
            "conflict resolution attempt": [
                resp(calls=[tc("r1", "read_file", '{"path":"calculator.py"}')]),
                resp(calls=[tc("r2", "write_file",
                               json.dumps({"path": "calculator.py",
                                           "content": CALC_RESOLVED}))]),
                resp(calls=[tc("r3", "run_command",
                               '{"cmd":"git add -A && git commit -m \'resolve conflict\'"}')]),
                resp(calls=[tc("r4", "mark_task_complete", '{"summary":"merged hunks"}')]),
            ],
            # Tier-1 targeted review: strict-JSON verdict, NO tool calls.
            "resolution hunk": [resp(content=SOUND_VERDICT)],
        },
    )
    descriptor = await _pump_to_stop(ctx, gateway)
    assert descriptor == "local_green"

    # Both tasks completed; the later-seq one (B, integrated second) hit the
    # conflict and was saved by the resolution loop.
    fresh_b = await repo.get_task(ctx.db, task_b.id)
    fresh_a = await repo.get_task(ctx.db, task_a.id)
    assert fresh_b is not None and fresh_b.status is TaskStatus.COMPLETED
    assert fresh_a is not None and fresh_a.status is TaskStatus.COMPLETED

    # The wave branch history contains a real merge commit (two parents).
    wave_branch = f"wave/{ctx.run.id[:8]}/w0"
    merges = (await git(ctx.repo_path, "rev-list", "--merges", wave_branch)).split()
    assert merges, f"expected a merge commit on {wave_branch}"
    parents = (await git(ctx.repo_path, "rev-list", "--parents", "-n", "1", merges[0])).split()
    assert len(parents) == 3  # merge sha + two parents

    # Audit event written.
    assert await repo.get_latest_event(ctx.db, ctx.run.id, "conflict_resolved") is not None

    # The hunk-review call happened: first user message mentions
    # "resolution hunk" and the call carried NO tool calls.
    review_calls = [
        m
        for m in gateway.calls
        if "resolution hunk" in first_user_content(m)
    ]
    assert review_calls, "Tier-1 resolution-hunk review must have run"
    assert not any(getattr(m, "tool_calls", None) for entry in review_calls for m in entry)

    # Final content on the run branch is the resolved version.
    assert (await git(ctx.repo_path, "show", "run/e2e1:calculator.py")) == CALC_RESOLVED


async def test_sc15_resolution_exhausted_drops_task_and_escalates(make_ctx: MakeCtx) -> None:
    """SC-15 failure path: both resolution attempts fail to commit, so the
    offending task is DROPPED, the wave branch is rolled back to the first
    task's tip, and the run escalates with an error notification."""
    ctx, (task_a, task_b) = await _seed_conflicting_run(make_ctx)

    # Two scripted attempts (cap = settings.limits.conflict_resolution_attempts
    # = 2), each a read turn then mark_task_complete WITHOUT any commit.
    def failed_attempt(tag: str) -> list[object]:
        return [
            resp(calls=[tc(f"{tag}-read", "read_file", '{"path":"calculator.py"}')]),
            resp(calls=[tc(f"{tag}-done", "mark_task_complete",
                           '{"summary":"could not resolve"}')]),
        ]

    gateway = FakeGateway(
        responses=[],
        routes={
            TASK_A_TITLE: write_commit_of("calculator.py", CALC_A, tc_id="a"),
            TASK_B_TITLE: write_commit_of("calculator.py", CALC_B, tc_id="b"),
            "conflict resolution attempt": [
                *failed_attempt("x1"),
                *failed_attempt("x2"),
            ],
        },
    )
    descriptor = await _pump_to_stop(ctx, gateway)
    assert descriptor == "escalated"

    fresh_run = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh_run is not None and fresh_run.status is RunStatus.ESCALATED

    fresh_b = await repo.get_task(ctx.db, task_b.id)
    assert fresh_b is not None and fresh_b.status is TaskStatus.DROPPED
    # The first task's merge survived the rollback.
    fresh_a = await repo.get_task(ctx.db, task_a.id)
    assert fresh_a is not None and fresh_a.status is TaskStatus.COMPLETED

    # Error notification was sent.
    assert any(
        level == "error" or "escalat" in (title + body).lower()
        for level, title, body in ctx.notifier.calls
    )

    # Wave branch tip rolled back to the pre-merge tip == first task's commit
    # (its calculator.py content, no merge commits on the branch).
    wave_branch = f"wave/{ctx.run.id[:8]}/w0"
    tip = (await git(ctx.repo_path, "rev-parse", wave_branch)).strip()
    assert (await git(ctx.repo_path, "show", f"{tip}:calculator.py")) == CALC_A
    assert not (await git(ctx.repo_path, "rev-list", "--merges", wave_branch)).split()

    # No resolution ever succeeded.
    assert await repo.get_latest_event(ctx.db, ctx.run.id, "conflict_resolved") is None
