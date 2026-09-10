"""Token-usage rows: pre-dispatch estimates, actuals reconciliation, rollups."""

from __future__ import annotations

from typing import Any

from girder.db.engine import Database
from girder.util import utcnow_iso


async def insert_token_usage(
    db: Database,
    *,
    run_id: str | None,
    attempt_id: str | None,
    model_role: str,
    model_id: str,
    estimated_before_call: float,
) -> int:
    """Pre-dispatch estimate row (impl-plan §6.8 step 2): state is persisted
    before the model call goes out; actuals overwrite it in reconcile."""
    cur = await db.execute(
        "INSERT INTO token_usage (attempt_id, run_id, model_role, model_id,"
        " prompt_tokens, completion_tokens, cost_usd, estimated_before_call, created_at)"
        " VALUES (?, ?, ?, ?, 0, 0, 0.0, ?, ?)",
        (attempt_id, run_id, model_role, model_id, estimated_before_call, utcnow_iso()),
    )
    return int(cur.lastrowid or 0)


async def update_token_usage_actual(
    db: Database,
    usage_id: int,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
) -> None:
    """Reconcile a pre-dispatch estimate row with the call's actual usage."""
    await db.execute(
        "UPDATE token_usage SET prompt_tokens = ?, completion_tokens = ?, cost_usd = ?"
        " WHERE id = ?",
        (prompt_tokens, completion_tokens, cost_usd, usage_id),
    )


async def list_token_usage_for_run(db: Database, run_id: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT id, run_id, attempt_id, model_role, model_id, prompt_tokens,"
        " completion_tokens, cost_usd, estimated_before_call, created_at"
        " FROM token_usage WHERE run_id = ? ORDER BY id",
        (run_id,),
    )
    return [dict(r) for r in rows]


async def list_token_usage_for_attempt(db: Database, attempt_id: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT id, model_role, model_id, prompt_tokens, completion_tokens,"
        " cost_usd, estimated_before_call, created_at FROM token_usage"
        " WHERE attempt_id = ? ORDER BY id",
        (attempt_id,),
    )
    return [dict(r) for r in rows]


async def sum_token_usage_for_run(db: Database, run_id: str) -> list[dict[str, Any]]:
    """Per-model-role usage totals behind the spend dashboard (§10)."""
    rows = await db.fetchall(
        "SELECT model_role, model_id, COUNT(*) AS calls,"
        " SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens,"
        " SUM(cost_usd) AS cost_usd, SUM(estimated_before_call) AS estimated_usd"
        " FROM token_usage WHERE run_id = ? GROUP BY model_role, model_id ORDER BY model_role",
        (run_id,),
    )
    return [dict(r) for r in rows]


async def sum_token_usage_by_task_for_run(db: Database, run_id: str) -> list[dict[str, Any]]:
    """Per-task usage rollup behind the spend dashboard (plan.md Phase 5):
    token_usage rows aggregate through their attempt's task."""
    rows = await db.fetchall(
        "SELECT t.id AS task_id, t.title AS title,"
        " SUM(u.prompt_tokens) AS prompt_tokens, SUM(u.completion_tokens) AS completion_tokens,"
        " SUM(u.cost_usd) AS cost_usd, SUM(u.estimated_before_call) AS estimated_usd"
        " FROM token_usage u"
        " JOIN attempts a ON a.id = u.attempt_id"
        " JOIN tasks t ON t.id = a.task_id"
        " WHERE u.run_id = ? GROUP BY t.id, t.title ORDER BY t.seq",
        (run_id,),
    )
    return [dict(r) for r in rows]
