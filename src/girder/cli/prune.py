"""The ``girder prune`` command (WP 13.2).

Split out of the former single-file ``girder/cli.py`` (see git history for
the original); zero behavior change.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import aiosqlite

from girder.db.engine import Database
from girder.db.models import TERMINAL_RUN_STATUSES

# No FK in migrations 001-013 declares ON DELETE CASCADE, and the engine runs
# with ``PRAGMA foreign_keys=ON`` — deleting a run with children would fail.
# Prune therefore deletes child rows explicitly, deepest dependents first,
# inside one transaction (``db.tx()``). Rows linked only by bare TEXT columns
# (no FK) — steering_events, integrity_violations, notifications_log — are
# included so nothing referencing a pruned run survives.


@dataclass
class PruneReport:
    """What one prune pass deleted (or would delete, in dry-run mode)."""

    run_ids: list[str]
    child_rows: dict[str, int] = field(default_factory=dict)

    @property
    def run_count(self) -> int:
        return len(self.run_ids)

    def summary(self, *, dry_run: bool) -> str:
        verb = "would delete" if dry_run else "deleted"
        lines = [f"{verb} {self.run_count} run(s) past the prune horizon:"]
        lines.extend(f"  {run_id}" for run_id in self.run_ids)
        lines.append(f"{verb} child rows:")
        for table, count in self.child_rows.items():
            lines.append(f"  {table}: {count}")
        return "\n".join(lines)


# Run-id scoping subqueries. _RUN_SCOPE selects the ids to prune; _ATTEMPT_SCOPE
# selects the attempts belonging to those runs via waves -> tasks -> attempts.
_RUN_SCOPE = "SELECT id FROM prune_run_ids"
_ATTEMPT_SCOPE = (
    "SELECT a.id FROM attempts a"
    " JOIN tasks t ON a.task_id = t.id"
    " JOIN waves w ON t.wave_id = w.id"
    f" WHERE w.run_id IN ({_RUN_SCOPE})"
)
_TASK_SCOPE = (
    "SELECT t.id FROM tasks t"
    " JOIN waves w ON t.wave_id = w.id"
    f" WHERE w.run_id IN ({_RUN_SCOPE})"
)

# (table, where-clause) pairs in dependency-safe delete order. FK targets are
# always emptied before their parents: attempt-level rows, then attempts, then
# tasks, then waves, then run-level rows, then runs themselves.
_PRUNE_TABLES: list[tuple[str, str]] = [
    ("worktrees", f"attempt_id IN ({_ATTEMPT_SCOPE})"),
    ("redaction_log", f"attempt_id IN ({_ATTEMPT_SCOPE})"),
    ("tool_calls", f"attempt_id IN ({_ATTEMPT_SCOPE})"),
    ("token_usage", f"attempt_id IN ({_ATTEMPT_SCOPE}) OR run_id IN ({_RUN_SCOPE})"),
    ("attempt_prompts", f"attempt_id IN ({_ATTEMPT_SCOPE}) OR run_id IN ({_RUN_SCOPE})"),
    ("attempt_diffs", f"attempt_id IN ({_ATTEMPT_SCOPE}) OR run_id IN ({_RUN_SCOPE})"),
    ("attempts", f"task_id IN ({_TASK_SCOPE})"),
    ("spec_amendments", f"run_id IN ({_RUN_SCOPE}) OR task_id IN ({_TASK_SCOPE})"),
    ("tasks", f"wave_id IN (SELECT id FROM waves WHERE run_id IN ({_RUN_SCOPE}))"),
    ("waves", f"run_id IN ({_RUN_SCOPE})"),
    ("ci_check_results", f"run_id IN ({_RUN_SCOPE})"),
    ("agent_events", f"run_id IN ({_RUN_SCOPE}) OR attempt_id IN ({_ATTEMPT_SCOPE})"),
    ("integrity_violations", f"run_id IN ({_RUN_SCOPE})"),
    ("steering_events", f"run_id IN ({_RUN_SCOPE})"),
    ("notifications_log", f"run_id IN ({_RUN_SCOPE})"),
    ("clarification_sessions", f"run_id IN ({_RUN_SCOPE})"),
]


async def _prune_run_ids(db: Database, older_than_days: int) -> list[str]:
    """Terminal-status runs whose created_at is older than the horizon."""
    cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).replace(
        microsecond=0
    ).isoformat()
    statuses = sorted(s.value for s in TERMINAL_RUN_STATUSES)
    placeholders = ",".join("?" * len(statuses))
    rows = await db.fetchall(
        f"SELECT id FROM runs WHERE status IN ({placeholders})"
        " AND created_at < ? ORDER BY created_at",
        (*statuses, cutoff),
    )
    return [str(r["id"]) for r in rows]


async def _count_prune_rows(conn: aiosqlite.Connection, run_ids: list[str]) -> dict[str, int]:
    """Per-child-table row counts that a prune of *run_ids* would remove."""
    counts: dict[str, int] = {}
    for table, where in _PRUNE_TABLES:
        cur = await conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}")
        row = await cur.fetchone()
        counts[table] = int(row[0]) if row is not None else 0
    return counts


async def prune_runs(db: Database, *, older_than_days: int, dry_run: bool) -> PruneReport:
    """Delete terminal runs older than the horizon (or just report them).

    Both modes scope the victim set into a TEMP table first so every count and
    delete below shares one exact definition of "the runs being pruned".
    Dry-run never issues a DELETE against the main database.
    """
    run_ids = await _prune_run_ids(db, older_than_days)
    conn = db.conn
    await conn.execute("CREATE TEMP TABLE IF NOT EXISTS prune_run_ids (id TEXT PRIMARY KEY)")
    await conn.execute("DELETE FROM prune_run_ids")
    if run_ids:
        await conn.executemany(
            "INSERT INTO prune_run_ids (id) VALUES (?)", [(rid,) for rid in run_ids]
        )
    if dry_run:
        counts = await _count_prune_rows(conn, run_ids)
        await conn.execute("DROP TABLE IF EXISTS temp.prune_run_ids")
        return PruneReport(run_ids=run_ids, child_rows=counts)
    async with db.tx() as tx:
        counts = await _count_prune_rows(tx, run_ids)
        for table, where in _PRUNE_TABLES:
            await tx.execute(f"DELETE FROM {table} WHERE {where}")
        await tx.execute(f"DELETE FROM runs WHERE id IN ({_RUN_SCOPE})")
        await tx.execute("DELETE FROM prune_run_ids")
        await tx.execute("DROP TABLE IF EXISTS temp.prune_run_ids")
    return PruneReport(run_ids=run_ids, child_rows=counts)


async def cmd_prune(args: argparse.Namespace) -> int:
    # Monkeypatch indirection: read through the package namespace.
    from girder import cli

    db = await cli._open_db(args)
    try:
        report = await prune_runs(
            db, older_than_days=args.older_than_days, dry_run=args.dry_run
        )
    finally:
        await db.close()
    print(report.summary(dry_run=args.dry_run))
    return 0
