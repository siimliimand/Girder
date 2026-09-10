"""Shared helpers and SQL fragments for the ``girder.db.repo`` package."""

from __future__ import annotations

from typing import Any

from girder.db.models import TERMINAL_ATTEMPT_STATUSES, TERMINAL_TASK_STATUSES

_TERMINAL_ATTEMPT_SQL = ",".join(f"'{s.value}'" for s in TERMINAL_ATTEMPT_STATUSES)
_TERMINAL_TASK_SQL = ",".join(f"'{s.value}'" for s in TERMINAL_TASK_STATUSES)


def _validate_fields(table: str, fields: dict[str, Any], whitelist: frozenset[str]) -> None:
    for key in fields:
        if key in whitelist:
            continue
        if key == "status":
            raise ValueError(
                f"{table} status changes must go through girder.fsm.transition"
            )
        raise ValueError(f"unknown or non-writable {table} column: {key}")
