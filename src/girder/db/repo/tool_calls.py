"""Tool-call audit rows and the held/scope-violation count (impl-plan §8)."""

from __future__ import annotations

from typing import Any

from girder.db.engine import Database
from girder.util import utcnow_iso


async def insert_tool_call(
    db: Database,
    *,
    attempt_id: str,
    tool_name: str,
    input_json: str,
    output_redacted: str | None = None,
    duration_ms: int | None = None,
    scope_violation: bool = False,
    held: bool = False,
    verdict: str | None = None,
) -> int:
    """Insert one tool-call audit row. ``verdict`` (migration 011) is the
    ScopeGuard decision — "allow", "allow_logged" or "violation" — persisted
    for post-mortem auditability."""
    cur = await db.execute(
        "INSERT INTO tool_calls (attempt_id, ts, tool_name, input_json,"
        " output_blob_redacted, duration_ms, scope_violation, held, verdict)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            attempt_id,
            utcnow_iso(),
            tool_name,
            input_json,
            output_redacted,
            duration_ms,
            int(scope_violation),
            int(held),
            verdict,
        ),
    )
    return int(cur.lastrowid or 0)


async def list_tool_calls_for_attempt(db: Database, attempt_id: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT id, attempt_id, ts, tool_name, input_json, output_blob_redacted,"
        " duration_ms, scope_violation, held, verdict"
        " FROM tool_calls WHERE attempt_id = ? ORDER BY id",
        (attempt_id,),
    )
    return [dict(r) for r in rows]


async def count_held_tool_calls(db: Database, attempt_id: str) -> int:
    """Count held or scope-violating tool calls for one attempt (impl-plan §8).

    A ``held=1`` row means the registry refused to execute the call; any such
    row taints the whole attempt — completion may never rest on work the
    orchestrator never ran. Used by the verify step to fail the attempt
    without retry before any merge can happen."""
    row = await db.fetchone(
        "SELECT COUNT(*) AS n FROM tool_calls WHERE attempt_id = ?"
        " AND (scope_violation = 1 OR held = 1)",
        (attempt_id,),
    )
    return int(row["n"]) if row is not None else 0
