"""Task rows: creation, lookup, field updates, scope-glob widening."""

from __future__ import annotations

import json
from typing import Any

from aiosqlite import Row

from girder.db.engine import Database
from girder.db.models import Task, TaskStatus, TaskType
from girder.db.repo._common import _validate_fields
from girder.util import new_id

_TASK_WRITABLE_FIELDS = frozenset(
    {
        "test_content_hash",
        "attempts_used",
        "title",
        "scope_globs_json",
        "spec_slice_md",
        "depends_on_json",
        "wave_id",
    }
)


async def create_task(
    db: Database,
    wave_id: str,
    seq: int,
    title: str,
    task_type: TaskType,
    *,
    scope_globs: list[str] | None = None,
    spec_slice_md: str = "",
    depends_on: list[str] | None = None,
) -> Task:
    task = Task(
        id=new_id(),
        wave_id=wave_id,
        seq=seq,
        title=title,
        task_type=task_type,
        status=TaskStatus.PENDING,
        scope_globs=scope_globs or [],
        spec_slice_md=spec_slice_md,
        depends_on=depends_on or [],
    )
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO tasks (id, wave_id, seq, title, task_type, scope_globs_json,"
            " spec_slice_md, status, attempts_used, depends_on_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
            (
                task.id,
                wave_id,
                seq,
                title,
                task_type.value,
                json.dumps(task.scope_globs),
                spec_slice_md,
                json.dumps(task.depends_on),
            ),
        )
    return task


def _row_to_task(r: Row) -> Task:
    return Task(
        id=r["id"],
        wave_id=r["wave_id"],
        seq=r["seq"],
        title=r["title"],
        task_type=TaskType(r["task_type"]),
        status=TaskStatus(r["status"]),
        scope_globs=json.loads(r["scope_globs_json"]),
        spec_slice_md=r["spec_slice_md"],
        test_content_hash=r["test_content_hash"],
        attempts_used=r["attempts_used"],
        depends_on=json.loads(r["depends_on_json"]),
    )


async def get_task(db: Database, task_id: str) -> Task | None:
    r = await db.fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return _row_to_task(r) if r else None


async def list_tasks_for_run(db: Database, run_id: str) -> list[Task]:
    rows = await db.fetchall(
        "SELECT t.* FROM tasks t JOIN waves w ON t.wave_id = w.id"
        " WHERE w.run_id = ? ORDER BY w.sequence_order, t.seq",
        (run_id,),
    )
    return [_row_to_task(r) for r in rows]


async def list_tasks_for_wave(db: Database, wave_id: str) -> list[Task]:
    rows = await db.fetchall(
        "SELECT * FROM tasks WHERE wave_id = ? ORDER BY seq", (wave_id,)
    )
    return [_row_to_task(r) for r in rows]


async def update_task_fields(db: Database, task_id: str, **fields: Any) -> None:
    if not fields:
        return
    _validate_fields("tasks", fields, _TASK_WRITABLE_FIELDS)
    cols = ", ".join(f"{k} = ?" for k in fields)
    await db.execute(f"UPDATE tasks SET {cols} WHERE id = ?", (*fields.values(), task_id))


async def find_tasks_in_statuses(db: Database, statuses: list[str]) -> list[Task]:
    """Tasks whose status is in *statuses* (as raw strings, e.g. from TaskStatus)."""
    placeholders = ",".join("?" for _ in statuses)
    rows = await db.fetchall(
        f"SELECT * FROM tasks WHERE status IN ({placeholders})", tuple(statuses)
    )
    return [_row_to_task(r) for r in rows]


async def update_task_scope_globs(db: Database, task_id: str, globs: list[str]) -> None:
    """Union *globs* into the task's scope_globs (§8.4: an approved amendment
    may widen scope; union with the existing globs is the safe default — an
    amendment never narrows what was already authorized)."""
    task = await get_task(db, task_id)
    if task is None:
        raise ValueError(f"task {task_id} not found")
    merged = list(dict.fromkeys([*task.scope_globs, *globs]))
    await update_task_fields(db, task_id, scope_globs_json=json.dumps(merged))
