"""SC-02: tests are immutable — the ToolRegistry holds out-of-scope test
writes, and a direct test-tree mutation on a code_change task is an
integrity violation that never merges and fails the run."""

from __future__ import annotations

import json

import pytest

from girder.db import repo
from girder.db.models import AttemptStatus, RunStatus, TaskStatus
from tests.e2e.conftest import FakeGateway, MakeCtx, git, resp, tc

pytestmark = pytest.mark.e2e

PROPOSAL = """\
---
schema: girder.openspec/v1
title: Add a util module
intent: A util module exists.
tasks:
  - id: add-util
    title: Add util module
    type: code_change
    scope_globs: ["src/**"]
    success_criteria: ["src/util.py exists"]
    depends_on: []
  - id: sneaky-tests
    title: Touch the test tree
    type: code_change
    scope_globs: ["src/**", "tests/**"]
    success_criteria: ["nothing to do"]
    depends_on: []
---
Narrative.
"""


async def test_sc02_test_write_held_then_direct_mutation_fails_run(make_ctx: MakeCtx) -> None:
    ctx = await make_ctx(PROPOSAL)
    # Sprint 5: both tasks run concurrently in wave 0, so each task's turns
    # are routed by title instead of relying on FIFO order.
    gateway = FakeGateway(
        responses=[],
        routes={
            # Task 1: first try to tamper with the frozen test file — held.
            "Add util module": [
                resp(
                    calls=[
                        tc(
                            "1",
                            "write_file",
                            json.dumps(
                                {
                                    "path": "tests/test_arithmetic.py",
                                    "content": "def test_hacked():\n    assert False\n",
                                }
                            ),
                        )
                    ]
                ),
                # ...then comply legitimately.
                resp(
                    calls=[
                        tc(
                            "2",
                            "write_file",
                            json.dumps({"path": "src/util.py", "content": "x = 1\n"}),
                        )
                    ]
                ),
                resp(
                    calls=[tc("2-c", "run_command", '{"cmd":"git add -A && git commit -m work"}')]
                ),
                resp(calls=[tc("2-d", "mark_task_complete", '{"summary":"added util"}')]),
            ],
            # Task 2 (scope INCLUDES tests/**): the registry lets the write
            # through, but the mechanical audit must fail it without retry.
            "Touch the test tree": [
                resp(
                    calls=[
                        tc(
                            "3",
                            "write_file",
                            json.dumps(
                                {
                                    "path": "tests/new_test.py",
                                    "content": "def test_x():\n    pass\n",
                                }
                            ),
                        )
                    ]
                ),
                resp(
                    calls=[
                        tc("3-c", "run_command", '{"cmd":"git add -A && git commit -m sneaky"}')
                    ]
                ),
                resp(calls=[tc("3-d", "mark_task_complete", '{"summary":"sneaky"}')]),
            ],
        },
    )
    # Baseline for "nothing merged": the run branch's own tip (it carries the
    # spec-freeze commit, so main's tip is not a meaningful comparator).
    tip_before = (await git(ctx.repo_path, "rev-parse", ctx.run.branch)).strip()

    descriptor = await ctx.engine(gateway).run_to_completion(ctx.run.id)
    assert descriptor == "failed"

    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None and fresh.status is RunStatus.FAILED
    tasks = await repo.list_tasks_for_run(ctx.db, ctx.run.id)
    # Sprint 5 wave mode: both tasks run concurrently in wave 0; task 2's
    # integrity violation fails the run BEFORE integration, so task 1 is left
    # verified-but-unintegrated and the run branch never moves.
    assert [t.status for t in tasks] == [TaskStatus.VERIFY_PASSED, TaskStatus.FAILED]
    tip_after = (await git(ctx.repo_path, "rev-parse", fresh.branch)).strip()
    assert tip_after == tip_before  # an integrity-violating run never merges

    # Task 1's attempt: exactly one held tool_call row (the test write)
    attempts1 = await ctx.db.fetchall(
        "SELECT id, status FROM attempts WHERE task_id = ? ORDER BY attempt_num", (tasks[0].id,)
    )
    assert len(attempts1) == 1
    held = await ctx.db.fetchall(
        "SELECT tool_name, input_json FROM tool_calls WHERE attempt_id = ? AND held = 1",
        (attempts1[0]["id"],),
    )
    assert len(held) == 1
    assert held[0]["tool_name"] == "write_file"
    assert "tests/test_arithmetic.py" in held[0]["input_json"]
    # ...and the tamper never landed on the branch
    show = await git(ctx.repo_path, "show", f"{fresh.branch}:tests/test_arithmetic.py")
    assert "test_hacked" not in show

    # Task 2: integrity violation, no retry, recorded on run + audit tables
    attempts2 = await ctx.db.fetchall(
        "SELECT id, status FROM attempts WHERE task_id = ? ORDER BY attempt_num", (tasks[1].id,)
    )
    assert [a["status"] for a in attempts2] == [AttemptStatus.INTEGRITY_VIOLATION.value]
    kinds = {
        r["kind"] for r in await ctx.db.fetchall("SELECT kind FROM integrity_violations")
    }
    assert {"out_of_scope_write", "test_path_modified", "content_hash_mismatch"} <= kinds
    assert ctx.notifier.calls  # failure path notified
