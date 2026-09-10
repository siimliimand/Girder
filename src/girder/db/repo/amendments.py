"""Spec amendments (sprint 3): proposal, resolution, rejected guidance."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from aiosqlite import Row

from girder.db.engine import Database
from girder.util import new_id, utcnow_iso


@dataclass
class SpecAmendment:
    id: str
    run_id: str
    task_id: str | None
    reason: str
    suggested_change: str
    status: str = "pending"
    guidance: str | None = None
    new_spec_hash: str | None = None
    resolved_at: str | None = None
    # Migration 013: scope globs the resolver wants unioned into the amended
    # task on approve (§8.4); None/[] for amendments that don't touch scope.
    scope_globs: list[str] = field(default_factory=list)


_RESOLVED_AMENDMENT_STATUSES = frozenset({"approved", "rejected", "aborted"})


async def create_spec_amendment(
    db: Database, run_id: str, reason: str, suggested_change: str, *,
    task_id: str | None = None,
    scope_globs: list[str] | None = None,
) -> SpecAmendment:
    amendment = SpecAmendment(
        id=new_id(), run_id=run_id, task_id=task_id, reason=reason,
        suggested_change=suggested_change, scope_globs=scope_globs or [],
    )
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO spec_amendments (id, run_id, task_id, reason, suggested_change,"
            " status, scope_globs_json)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (
                amendment.id, run_id, task_id, reason, suggested_change,
                json.dumps(amendment.scope_globs),
            ),
        )
    return amendment


def _row_to_spec_amendment(r: Row) -> SpecAmendment:
    return SpecAmendment(
        id=r["id"],
        run_id=r["run_id"],
        task_id=r["task_id"],
        reason=r["reason"],
        suggested_change=r["suggested_change"],
        status=r["status"],
        guidance=r["guidance"],
        new_spec_hash=r["new_spec_hash"],
        resolved_at=r["resolved_at"],
        scope_globs=json.loads(r["scope_globs_json"]) if r["scope_globs_json"] else [],
    )


async def update_spec_amendment_scope_globs(
    db: Database, amendment_id: str, globs: list[str]
) -> None:
    """Persist resolver-supplied scope globs onto the amendment row (§8.4)."""
    async with db.tx() as conn:
        await conn.execute(
            "UPDATE spec_amendments SET scope_globs_json = ? WHERE id = ?",
            (json.dumps(list(globs)), amendment_id),
        )


async def get_spec_amendment(db: Database, amendment_id: str) -> SpecAmendment | None:
    r = await db.fetchone("SELECT * FROM spec_amendments WHERE id = ?", (amendment_id,))
    return _row_to_spec_amendment(r) if r else None


async def list_amendments_for_run(db: Database, run_id: str) -> list[SpecAmendment]:
    rows = await db.fetchall(
        "SELECT * FROM spec_amendments WHERE run_id = ? ORDER BY rowid", (run_id,)
    )
    return [_row_to_spec_amendment(r) for r in rows]


async def get_pending_amendment(db: Database, run_id: str) -> SpecAmendment | None:
    """Newest still-pending amendment for a run, if any."""
    r = await db.fetchone(
        "SELECT * FROM spec_amendments WHERE run_id = ? AND status = 'pending'"
        " ORDER BY rowid DESC LIMIT 1",
        (run_id,),
    )
    return _row_to_spec_amendment(r) if r else None


async def list_pending_amendments(db: Database) -> list[SpecAmendment]:
    """All still-pending amendments across every run, oldest first (§10
    Amendments inbox page)."""
    rows = await db.fetchall(
        "SELECT * FROM spec_amendments WHERE status = 'pending' ORDER BY rowid"
    )
    return [_row_to_spec_amendment(r) for r in rows]


async def get_rejected_guidance(db: Database, run_id: str, task_id: str) -> str | None:
    """Guidance from the most recent rejected amendment for this run+task
    (falling back to a run-level amendment with ``task_id IS NULL``).

    Returns None when no rejected amendment carried guidance. Used by the
    TaskEngine to relay user-authored rejection guidance into the resumed
    attempt's trusted steering (impl-plan §6.9)."""
    r = await db.fetchone(
        "SELECT guidance FROM spec_amendments"
        " WHERE run_id = ? AND status = 'rejected' AND guidance IS NOT NULL"
        " AND (task_id = ? OR task_id IS NULL)"
        " ORDER BY (task_id = ?) DESC, rowid DESC LIMIT 1",
        (run_id, task_id, task_id),
    )
    return r["guidance"] if r else None


async def resolve_spec_amendment(
    db: Database,
    amendment_id: str,
    *,
    status: str,
    guidance: str | None = None,
    new_spec_hash: str | None = None,
) -> None:
    """Resolve a pending amendment. spec_amendments.status is a plain CHECK
    column (not fsm-guarded), so this is the sole writer of its terminal states."""
    if status not in _RESOLVED_AMENDMENT_STATUSES:
        raise ValueError(
            f"amendment status must be one of {sorted(_RESOLVED_AMENDMENT_STATUSES)},"
            f" got {status!r}"
        )
    async with db.tx() as conn:
        await conn.execute(
            "UPDATE spec_amendments SET status = ?, guidance = ?, new_spec_hash = ?,"
            " resolved_at = ? WHERE id = ?",
            (status, guidance, new_spec_hash, utcnow_iso(), amendment_id),
        )
