"""Integrity-violation rows (impl-plan §4.3)."""

from __future__ import annotations

import json
from typing import Any

from girder.db.engine import Database
from girder.util import utcnow_iso


async def insert_integrity_violation(
    db: Database,
    run_id: str,
    kind: str,
    detail: dict[str, Any],
    *,
    task_id: str | None = None,
    attempt_id: str | None = None,
) -> None:
    """Record an integrity violation and bump the run's counter.

    Integrity violations are a distinct class from ordinary test failures
    (impl-plan §4.3): they block merge at every autonomy tier and reset the
    project's clean-merge streak.
    """
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO integrity_violations (run_id, task_id, attempt_id, kind,"
            " detail_json, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, task_id, attempt_id, kind, json.dumps(detail), utcnow_iso()),
        )
        await conn.execute(
            "UPDATE runs SET integrity_violations = integrity_violations + 1,"
            " updated_at = ? WHERE id = ?",
            (utcnow_iso(), run_id),
        )
        await conn.execute(
            "UPDATE projects SET clean_merge_streak = 0, updated_at = ? WHERE id ="
            " (SELECT project_id FROM runs WHERE id = ?)",
            (utcnow_iso(), run_id),
        )


async def list_integrity_violations_for_run(db: Database, run_id: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT id, run_id, task_id, attempt_id, kind, detail_json, ts"
        " FROM integrity_violations WHERE run_id = ? ORDER BY id",
        (run_id,),
    )
    return [dict(r) for r in rows]
