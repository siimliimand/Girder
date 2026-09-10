"""Wave rows: creation (with UNIQUE(run_id, sequence_order) race handling)."""

from __future__ import annotations

import sqlite3

from aiosqlite import Row

from girder.db.engine import Database
from girder.db.models import Wave
from girder.util import new_id


async def create_wave(db: Database, run_id: str, sequence_order: int) -> Wave:
    wave = Wave(id=new_id(), run_id=run_id, sequence_order=sequence_order)
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO waves (id, run_id, sequence_order, status) VALUES (?, ?, ?, 'pending')",
            (wave.id, run_id, sequence_order),
        )
    return wave


async def get_or_create_wave0(db: Database, run_id: str) -> Wave:
    r = await db.fetchone(
        "SELECT * FROM waves WHERE run_id = ? ORDER BY sequence_order LIMIT 1", (run_id,)
    )
    if r:
        return Wave(
            id=r["id"], run_id=r["run_id"], sequence_order=r["sequence_order"], status=r["status"]
        )
    return await create_wave(db, run_id, 0)


def _row_to_wave(r: Row) -> Wave:
    return Wave(
        id=r["id"], run_id=r["run_id"], sequence_order=r["sequence_order"], status=r["status"]
    )


async def get_or_create_wave(db: Database, run_id: str, sequence_order: int) -> Wave:
    """Fetch the wave at (run_id, sequence_order), creating it if missing.

    Concurrent creators race on the UNIQUE(run_id, sequence_order) constraint;
    the loser re-selects instead of crashing.
    """
    r = await db.fetchone(
        "SELECT * FROM waves WHERE run_id = ? AND sequence_order = ?",
        (run_id, sequence_order),
    )
    if r:
        return _row_to_wave(r)
    wave = Wave(id=new_id(), run_id=run_id, sequence_order=sequence_order)
    try:
        async with db.tx() as conn:
            await conn.execute(
                "INSERT INTO waves (id, run_id, sequence_order, status)"
                " VALUES (?, ?, ?, 'pending')",
                (wave.id, run_id, sequence_order),
            )
    except sqlite3.IntegrityError:
        existing = await db.fetchone(
            "SELECT * FROM waves WHERE run_id = ? AND sequence_order = ?",
            (run_id, sequence_order),
        )
        assert existing is not None  # the constraint winner's row
        return _row_to_wave(existing)
    return wave


async def list_waves_for_run(db: Database, run_id: str) -> list[Wave]:
    rows = await db.fetchall(
        "SELECT * FROM waves WHERE run_id = ? ORDER BY sequence_order", (run_id,)
    )
    return [_row_to_wave(r) for r in rows]


async def set_wave_status(db: Database, wave_id: str, status: str) -> None:
    """Direct status write (waves intentionally have no FSM entity); callers
    write their own audit events."""
    await db.execute("UPDATE waves SET status = ? WHERE id = ?", (status, wave_id))
