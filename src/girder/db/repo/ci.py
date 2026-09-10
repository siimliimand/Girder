"""CI check results and rolling-review query (Sprint 4)."""

from __future__ import annotations

from typing import Any

from girder.db.engine import Database
from girder.util import utcnow_iso


async def insert_ci_check_result(
    db: Database,
    run_id: str,
    check_name: str,
    status: str,
    *,
    conclusion: str | None = None,
    url: str | None = None,
    log_excerpt_redacted: str | None = None,
) -> int:
    """Persist one observed CI check (impl-plan §6.11: redacted excerpts only)."""
    async with db.tx() as conn:
        cur = await conn.execute(
            "INSERT INTO ci_check_results (run_id, check_name, status, conclusion, url,"
            " log_excerpt_redacted, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, check_name, status, conclusion, url, log_excerpt_redacted, utcnow_iso()),
        )
        lastrowid: Any = cur.lastrowid
        row_id = int(lastrowid)
    return row_id


async def list_ci_check_results_for_run(db: Database, run_id: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT * FROM ci_check_results WHERE run_id = ? ORDER BY id", (run_id,)
    )
    return [dict(r) for r in rows]


async def count_unreviewed_merges(db: Database, project_id: str) -> int:
    """Merged runs of *project* with no ``merge_reviewed`` event (§2.3 T1
    rolling review window: "pause new merges if I haven't reviewed the last N")."""
    row = await db.fetchone(
        "SELECT COUNT(*) AS n FROM runs r WHERE r.project_id = ? AND r.status = 'merged'"
        " AND NOT EXISTS (SELECT 1 FROM agent_events e WHERE e.run_id = r.id"
        " AND e.event_type = 'merge_reviewed')",
        (project_id,),
    )
    n: Any = row["n"] if row else 0
    return int(n)
