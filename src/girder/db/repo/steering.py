"""Steering events: exactly-once delivery into a running agent."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from girder.db.engine import Database
from girder.util import utcnow_iso


async def insert_steering_event(
    db: Database, run_id: str, kind: str, payload: Mapping[str, object]
) -> int:
    cur = await db.execute(
        "INSERT INTO steering_events (run_id, kind, payload_json, created_at)"
        " VALUES (?, ?, ?, ?)",
        (run_id, kind, json.dumps(payload), utcnow_iso()),
    )
    return int(cur.lastrowid or 0)


async def consume_steering_events(
    db: Database, run_id: str, *, kinds: list[str] | None = None
) -> list[dict[str, Any]]:
    """Return unconsumed events (oldest first, optional kind filter) and mark
    them consumed in one transaction — each event is delivered exactly once."""
    async with db.tx() as conn:
        sql = "SELECT * FROM steering_events WHERE run_id = ? AND consumed_at IS NULL"
        params: list[Any] = [run_id]
        if kinds is not None:
            sql += f" AND kind IN ({','.join('?' for _ in kinds)})"
            params.extend(kinds)
        sql += " ORDER BY id"
        async with conn.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        for r in rows:
            await conn.execute(
                "UPDATE steering_events SET consumed_at = ? WHERE id = ?",
                (utcnow_iso(), r["id"]),
            )
    return [
        {
            "id": r["id"],
            "run_id": r["run_id"],
            "kind": r["kind"],
            "payload": json.loads(r["payload_json"]),
            "created_at": r["created_at"],
        }
        for r in rows
    ]
