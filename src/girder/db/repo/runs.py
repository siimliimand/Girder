"""Run rows: creation, lookup, field updates, spend reconciliation, pause."""

from __future__ import annotations

from typing import Any

from aiosqlite import Row

from girder.db.engine import Database
from girder.db.models import Run, RunStatus
from girder.db.repo._common import _validate_fields
from girder.util import new_id, utcnow_iso

# Non-status columns each update_*_fields may write. Status is excluded on
# purpose: it is only mutable through girder.fsm.transition.
_RUN_WRITABLE_FIELDS = frozenset(
    {
        "spec_hash",
        "budget_cap_usd",
        "spend_usd",
        "projected_spend_usd",
        "baseline_run_id",
        "pr_number",
        "proposal_md",
        "branch",
        "integrity_violations",
        "paused",
    }
)


async def create_run(
    db: Database, project_id: str, intent: str, branch: str, budget_cap_usd: float
) -> Run:
    run = Run(
        id=new_id(),
        project_id=project_id,
        intent=intent,
        branch=branch,
        status=RunStatus.DRAFT,
        budget_cap_usd=budget_cap_usd,
    )
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO runs (id, project_id, intent, branch, status, budget_cap_usd,"
            " spend_usd, projected_spend_usd, integrity_violations, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?)",
            (
                run.id,
                project_id,
                intent,
                branch,
                run.status.value,
                budget_cap_usd,
                utcnow_iso(),
                utcnow_iso(),
            ),
        )
    return run


def _row_to_run(r: Row) -> Run:
    return Run(
        id=r["id"],
        project_id=r["project_id"],
        intent=r["intent"],
        branch=r["branch"],
        status=RunStatus(r["status"]),
        spec_hash=r["spec_hash"],
        budget_cap_usd=r["budget_cap_usd"],
        spend_usd=r["spend_usd"],
        projected_spend_usd=r["projected_spend_usd"],
        baseline_run_id=r["baseline_run_id"],
        pr_number=r["pr_number"],
        proposal_md=r["proposal_md"],
        integrity_violations=r["integrity_violations"],
        paused=bool(r["paused"]),
    )


async def get_run(db: Database, run_id: str) -> Run | None:
    r = await db.fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
    return _row_to_run(r) if r else None


async def update_run_fields(
    db: Database, run_id: str, *, updated_at: str | None = None, **fields: Any
) -> None:
    if not fields:
        return
    _validate_fields("runs", fields, _RUN_WRITABLE_FIELDS)
    cols = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(
        f"UPDATE runs SET {cols}, updated_at = ? WHERE id = ?",
        (*fields.values(), updated_at or utcnow_iso(), run_id),
    )


async def add_spend(db: Database, run_id: str, actual_delta: float, projected_total: float) -> None:
    """Reconcile spend: add actual cost, overwrite projected pre-flight figure."""
    async with db.tx() as conn:
        await conn.execute(
            "UPDATE runs SET spend_usd = spend_usd + ?, projected_spend_usd = ?,"
            " updated_at = ? WHERE id = ?",
            (actual_delta, projected_total, utcnow_iso(), run_id),
        )


async def set_run_paused(db: Database, run_id: str, paused: bool) -> None:
    """Persist the pump-level suspend flag (Phase 5 pause/resume steering)."""
    await update_run_fields(db, run_id, paused=int(paused))


# ------------------------------------------------------------------ web console (§10)


async def list_runs_for_project(db: Database, project_id: str) -> list[Run]:
    rows = await db.fetchall(
        "SELECT * FROM runs WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
    )
    return [_row_to_run(r) for r in rows]


async def list_runs_in_status(
    db: Database, status: str, *, project_id: str | None = None
) -> list[Run]:
    sql = "SELECT * FROM runs WHERE status = ?"
    params: list[Any] = [status]
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    sql += " ORDER BY updated_at"
    rows = await db.fetchall(sql, tuple(params))
    return [_row_to_run(r) for r in rows]
