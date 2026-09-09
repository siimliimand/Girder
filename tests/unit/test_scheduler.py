"""Unit tests for the sequential scheduler (WP 3.5)."""

from __future__ import annotations

from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run, Task, TaskStatus, TaskType
from girder.orchestrator.scheduler import Scheduler


async def _seed(db: Database, n_tasks: int) -> tuple[Project, Run, list[Task]]:
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/sched1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    tasks = [
        await repo.create_task(db, wave.id, seq, f"t{seq}", TaskType.CODE_CHANGE)
        for seq in range(1, n_tasks + 1)
    ]
    return project, run, tasks


async def _set_status(db: Database, task: Task, status: str) -> None:
    await db.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task.id))
    await db.conn.commit()


async def test_ordering_follows_wave_then_seq(db: Database) -> None:
    _project, run, tasks = await _seed(db, 3)
    scheduler = Scheduler(db)
    picked = await scheduler.next_task(run)
    assert picked is not None and picked.id == tasks[0].id
    await _set_status(db, tasks[0], TaskStatus.RUNNING.value)
    picked = await scheduler.next_task(run)
    assert picked is not None and picked.id == tasks[1].id


async def test_retry_scheduled_is_pickable_running_is_not(db: Database) -> None:
    _project, run, tasks = await _seed(db, 2)
    scheduler = Scheduler(db)
    await _set_status(db, tasks[0], TaskStatus.RETRY_SCHEDULED.value)
    picked = await scheduler.next_task(run)
    assert picked is not None and picked.id == tasks[0].id

    await _set_status(db, tasks[0], TaskStatus.AWAITING_AMENDMENT.value)
    await _set_status(db, tasks[1], TaskStatus.RUNNING.value)
    assert await scheduler.next_task(run) is None


async def test_pending_or_retry_count(db: Database) -> None:
    _project, run, tasks = await _seed(db, 3)
    scheduler = Scheduler(db)
    assert await scheduler.pending_or_retry_count(run) == 3
    await _set_status(db, tasks[0], TaskStatus.RUNNING.value)
    await _set_status(db, tasks[1], TaskStatus.RETRY_SCHEDULED.value)
    await _set_status(db, tasks[2], TaskStatus.COMPLETED.value)
    assert await scheduler.pending_or_retry_count(run) == 1


async def test_all_terminal_and_all_completed(db: Database) -> None:
    _project, run, tasks = await _seed(db, 2)
    scheduler = Scheduler(db)
    assert not await scheduler.all_terminal(run)
    assert not await scheduler.all_completed(run)

    await _set_status(db, tasks[0], TaskStatus.COMPLETED.value)
    await _set_status(db, tasks[1], TaskStatus.DROPPED.value)
    assert await scheduler.all_terminal(run)
    assert not await scheduler.all_completed(run)

    await _set_status(db, tasks[1], TaskStatus.COMPLETED.value)
    assert await scheduler.all_completed(run)


async def test_empty_run_is_not_terminal(db: Database) -> None:
    _project, run, _tasks = await _seed(db, 0)
    scheduler = Scheduler(db)
    assert not await scheduler.all_terminal(run)
    assert not await scheduler.all_completed(run)
    assert await scheduler.next_task(run) is None
