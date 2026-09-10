"""Attempt rows plus their per-attempt artifacts (prompts, diffs)."""

from __future__ import annotations

from typing import Any

from aiosqlite import Row

from girder.db.engine import Database
from girder.db.models import Attempt, AttemptDiff, AttemptPrompt, AttemptStatus
from girder.db.repo._common import _validate_fields
from girder.util import new_id, utcnow_iso

_ATTEMPT_WRITABLE_FIELDS = frozenset(
    {
        "exit_code",
        "turns_used",
        "worktree_path",
        "container_id",
        "failure_reason",
        "started_at",
        "ended_at",
    }
)


async def create_attempt(db: Database, task_id: str, base_commit: str) -> Attempt:
    """Create the next attempt for a task and bump attempts_used in one tx."""
    attempt_id = new_id()
    async with db.tx() as conn:
        async with conn.execute("SELECT attempts_used FROM tasks WHERE id = ?", (task_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            raise KeyError(f"task {task_id} not found")
        attempt_num = int(row["attempts_used"]) + 1
        await conn.execute(
            "UPDATE tasks SET attempts_used = ? WHERE id = ?", (attempt_num, task_id)
        )
        await conn.execute(
            "INSERT INTO attempts (id, task_id, attempt_num, base_commit, status)"
            " VALUES (?, ?, ?, ?, 'initialized')",
            (attempt_id, task_id, attempt_num, base_commit),
        )
    return Attempt(
        id=attempt_id,
        task_id=task_id,
        attempt_num=attempt_num,
        base_commit=base_commit,
        status=AttemptStatus.INITIALIZED,
    )


def _row_to_attempt(r: Row) -> Attempt:
    return Attempt(
        id=r["id"],
        task_id=r["task_id"],
        attempt_num=r["attempt_num"],
        base_commit=r["base_commit"],
        status=AttemptStatus(r["status"]),
        exit_code=r["exit_code"],
        turns_used=r["turns_used"],
        worktree_path=r["worktree_path"],
        container_id=r["container_id"],
        failure_reason=r["failure_reason"],
        started_at=r["started_at"],
        ended_at=r["ended_at"],
    )


async def get_attempt(db: Database, attempt_id: str) -> Attempt | None:
    r = await db.fetchone("SELECT * FROM attempts WHERE id = ?", (attempt_id,))
    return _row_to_attempt(r) if r else None


async def update_attempt_fields(db: Database, attempt_id: str, **fields: Any) -> None:
    if not fields:
        return
    _validate_fields("attempts", fields, _ATTEMPT_WRITABLE_FIELDS)
    cols = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(f"UPDATE attempts SET {cols} WHERE id = ?", (*fields.values(), attempt_id))


async def list_attempts_for_task(db: Database, task_id: str) -> list[Attempt]:
    rows = await db.fetchall(
        "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_num", (task_id,)
    )
    return [_row_to_attempt(r) for r in rows]


async def find_attempts_in_status(db: Database, status: AttemptStatus) -> list[Attempt]:
    rows = await db.fetchall("SELECT * FROM attempts WHERE status = ?", (status.value,))
    return [_row_to_attempt(r) for r in rows]


# -------------------------------------------------------------- attempt prompts


async def record_attempt_prompt(
    db: Database,
    attempt_id: str,
    turn: int,
    role: str,
    content: str,
    run_id: str | None = None,
) -> None:
    """Persist a per-turn prompt snapshot for the post-mortem explorer
    (plan.md Phase 5 task 5).

    ``content`` is stored exactly as given in ``attempt_prompts.content_redacted``
    — the CALLER is responsible for redacting secrets before calling this
    (the column name documents the expectation, enforcement lives upstream).
    """
    await db.execute(
        "INSERT INTO attempt_prompts (attempt_id, run_id, turn, role,"
        " content_redacted, ts) VALUES (?, ?, ?, ?, ?, ?)",
        (attempt_id, run_id, turn, role, content, utcnow_iso()),
    )


async def list_attempt_prompts(db: Database, attempt_id: str) -> list[AttemptPrompt]:
    """Return one attempt's prompt snapshots ordered by (turn, id)."""
    rows = await db.fetchall(
        "SELECT attempt_id, run_id, turn, role, content_redacted, ts"
        " FROM attempt_prompts WHERE attempt_id = ? ORDER BY turn, id",
        (attempt_id,),
    )
    return [
        AttemptPrompt(
            attempt_id=r["attempt_id"],
            run_id=r["run_id"],
            turn=int(r["turn"]),
            role=r["role"],
            content_redacted=r["content_redacted"],
            ts=r["ts"],
        )
        for r in rows
    ]


# --------------------------------------------------------------- attempt diffs


async def insert_attempt_diff(
    db: Database,
    *,
    attempt_id: str,
    run_id: str,
    base_commit: str,
    head_commit: str,
    diff_redacted: str,
    task_id: str | None = None,
) -> int:
    """Persist one attempt diff (migration 012, §10 diff viewer / plan.md §2.1).

    ``diff_redacted`` is stored exactly as given — the CALLER is responsible
    for redacting secrets before calling this (same contract as
    ``record_attempt_prompt``; enforcement lives upstream via the run's
    Redactor, typically ``redact_and_log(..., source_field="attempt_diff")``).
    """
    cur = await db.execute(
        "INSERT INTO attempt_diffs (attempt_id, task_id, run_id, base_commit,"
        " head_commit, diff_redacted, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (attempt_id, task_id, run_id, base_commit, head_commit, diff_redacted, utcnow_iso()),
    )
    return int(cur.lastrowid or 0)


def _row_to_attempt_diff(r: Row) -> AttemptDiff:
    return AttemptDiff(
        attempt_id=r["attempt_id"],
        task_id=r["task_id"],
        run_id=r["run_id"],
        base_commit=r["base_commit"],
        head_commit=r["head_commit"],
        diff_redacted=r["diff_redacted"],
        created_at=r["created_at"],
    )


async def list_attempt_diffs_for_run(db: Database, run_id: str) -> list[AttemptDiff]:
    """All persisted diffs of a run, oldest first."""
    rows = await db.fetchall(
        "SELECT attempt_id, task_id, run_id, base_commit, head_commit, diff_redacted,"
        " created_at FROM attempt_diffs WHERE run_id = ? ORDER BY id",
        (run_id,),
    )
    return [_row_to_attempt_diff(r) for r in rows]


async def latest_attempt_diff_for_task(db: Database, task_id: str) -> AttemptDiff | None:
    """Newest diff across a task's attempts (the current state of its branch)."""
    r = await db.fetchone(
        "SELECT attempt_id, task_id, run_id, base_commit, head_commit, diff_redacted,"
        " created_at FROM attempt_diffs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    )
    return _row_to_attempt_diff(r) if r else None
