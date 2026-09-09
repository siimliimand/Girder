"""SC-04: path-escape and out-of-scope writes are HELD by the ToolRegistry —
never executed anywhere — while a compliant in-scope write still merges."""

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
title: Add app module
intent: An app module exists.
tasks:
  - id: add-app
    title: Add app module
    type: code_change
    scope_globs: ["app/**"]
    success_criteria: ["app/real.py exists"]
    depends_on: []
---
Narrative.
"""


async def test_sc04_escape_and_out_of_scope_writes_held(make_ctx: MakeCtx) -> None:
    """Core SC-04: escape/out-of-scope writes are HELD (never executed anywhere)
    and logged. Because §8 blocks the merge at every tier for integrity
    violations, the held calls also taint the whole attempt: even though a
    later turn is clean, the attempt fails WITHOUT retry and nothing merges."""
    ctx = await make_ctx(PROPOSAL)
    gateway = FakeGateway(
        responses=[
            resp(
                calls=[
                    tc(
                        "1",
                        "write_file",
                        json.dumps({"path": "../../etc/evil.txt", "content": "pwned\n"}),
                    )
                ]
            ),
            resp(
                calls=[
                    tc(
                        "2",
                        "write_file",
                        json.dumps({"path": "src/evil.py", "content": "pwned\n"}),
                    )
                ]
            ),
            resp(
                calls=[
                    tc(
                        "3",
                        "write_file",
                        json.dumps({"path": "app/real.py", "content": "x = 1\n"}),
                    )
                ]
            ),
            resp(calls=[tc("3-c", "run_command", '{"cmd":"git add -A && git commit -m work"}')]),
            resp(calls=[tc("3-d", "mark_task_complete", '{"summary":"app added"}')]),
        ]
    )

    descriptor = await ctx.engine(gateway).run_to_completion(ctx.run.id)
    assert descriptor == "failed"  # held calls taint the attempt (impl-plan §8)

    fresh = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh is not None and fresh.status is RunStatus.FAILED
    tasks = await repo.list_tasks_for_run(ctx.db, ctx.run.id)
    assert [t.status for t in tasks] == [TaskStatus.FAILED]

    attempts = await ctx.db.fetchall(
        "SELECT id, status FROM attempts WHERE task_id = ?", (tasks[0].id,)
    )
    assert len(attempts) == 1  # integrity violation ⇒ no retry
    assert attempts[0]["status"] == AttemptStatus.INTEGRITY_VIOLATION.value
    held = await ctx.db.fetchall(
        "SELECT tool_name, input_json FROM tool_calls WHERE attempt_id = ? AND held = 1",
        (attempts[0]["id"],),
    )
    assert len(held) == 2
    targets = " ".join(h["input_json"] for h in held)
    assert "../../etc/evil.txt" in targets and "src/evil.py" in targets

    # the escape never landed anywhere on this machine
    assert not (ctx.repo_path / "etc" / "evil.txt").exists()
    assert not (ctx.repo_path.parent / "etc" / "evil.txt").exists()

    # the held calls block the merge at every tier: NOTHING merged — neither
    # the compliant write nor the held ones
    branch_files = await git(ctx.repo_path, "ls-tree", "-r", "--name-only", fresh.branch)
    assert "app/real.py" not in branch_files
    assert "src/evil.py" not in branch_files

    kinds = {
        r["kind"] for r in await ctx.db.fetchall(
            "SELECT kind FROM integrity_violations WHERE run_id = ?", (ctx.run.id,)
        )
    }
    assert "out_of_scope_write" in kinds
