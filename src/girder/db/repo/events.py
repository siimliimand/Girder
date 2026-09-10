"""Audit-log rows: agent events, redaction log, notifications log."""

from __future__ import annotations

import json
from typing import Any

from girder.db.engine import Database
from girder.util import utcnow_iso


async def insert_event(
    db: Database,
    event_type: str,
    payload: dict[str, Any],
    *,
    run_id: str | None = None,
    attempt_id: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO agent_events (ts, event_type, run_id, attempt_id, payload_json)"
        " VALUES (?, ?, ?, ?, ?)",
        (utcnow_iso(), event_type, run_id, attempt_id, json.dumps(payload)),
    )


async def insert_redaction(
    db: Database, source_field: str, pattern_matched: str, *, attempt_id: str | None = None
) -> None:
    """Record a redaction event for audit — never the redacted value (§8.6)."""
    await db.execute(
        "INSERT INTO redaction_log (attempt_id, source_field, pattern_matched, ts)"
        " VALUES (?, ?, ?, ?)",
        (attempt_id, source_field, pattern_matched, utcnow_iso()),
    )


async def insert_notification(
    db: Database,
    channel: str,
    payload_redacted: str,
    status: str,
    *,
    run_id: str | None = None,
) -> None:
    await db.execute(
        "INSERT INTO notifications_log (channel, payload_redacted, status, run_id, ts)"
        " VALUES (?, ?, ?, ?, ?)",
        (channel, payload_redacted, status, run_id, utcnow_iso()),
    )


async def list_notifications_for_run(db: Database, run_id: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT id, channel, payload_redacted, status, run_id, ts FROM notifications_log"
        " WHERE run_id = ? ORDER BY id",
        (run_id,),
    )
    return [dict(r) for r in rows]


async def get_latest_event(
    db: Database, run_id: str, event_type: str
) -> dict[str, Any] | None:
    """Most recent event of *event_type* for a run, as {id, payload, ts}."""
    r = await db.fetchone(
        "SELECT id, payload_json, ts FROM agent_events WHERE run_id = ? AND event_type = ?"
        " ORDER BY id DESC LIMIT 1",
        (run_id, event_type),
    )
    if r is None:
        return None
    return {"id": r["id"], "payload": json.loads(r["payload_json"]), "ts": r["ts"]}


async def get_first_transition_ts_to(
    db: Database, run_id: str, to_state: str
) -> str | None:
    """Timestamp of the FIRST ``state_transition`` event that moved the run
    into *to_state* (TEXT ISO, as stored), or ``None`` if it never did.

    Used for the CI poll deadline (impl-plan §6.11): the clock starts when the
    run first entered ``ci_running`` and is deliberately NOT reset by later
    ``ci_fixing → ci_running`` re-entries — the failure mode being guarded
    against is "CI stuck pending forever", not "fix cycles take long".
    """
    r = await db.fetchone(
        "SELECT ts FROM agent_events WHERE run_id = ? AND event_type = 'state_transition'"
        " AND json_extract(payload_json, '$.to') = ?"
        " ORDER BY id ASC LIMIT 1",
        (run_id, to_state),
    )
    return None if r is None else str(r["ts"])


async def list_events_for_run(
    db: Database, run_id: str, *, after_id: int = 0, limit: int = 500
) -> list[dict[str, Any]]:
    """Agent events of *run* ordered by id, tail-pollable via ``after_id`` (§10 SSE)."""
    rows = await db.fetchall(
        "SELECT id, ts, event_type, run_id, attempt_id, payload_json FROM agent_events"
        " WHERE run_id = ? AND id > ? ORDER BY id LIMIT ?",
        (run_id, after_id, limit),
    )
    return [dict(r) for r in rows]
