"""Sequential task scheduler (Sprint 3 v1, impl-plan §11 WP 3.5).

One task at a time, in (wave.sequence_order, task.seq) order. Tasks already
mid-flight (``running`` — an attempt is executing) or parked
(``awaiting_amendment``) are deliberately NOT re-picked: the pump is
single-threaded, so a ``running`` task seen here would mean either a leaked
state (recovery handles those at boot) or an amendment resume — the run
engine drives the resume case explicitly, not the scheduler.
"""

from __future__ import annotations

from girder.db import repo
from girder.db.engine import Database
from girder.db.models import TERMINAL_TASK_STATUSES, Run, Task, TaskStatus

_PICKABLE = frozenset({TaskStatus.PENDING, TaskStatus.RETRY_SCHEDULED})


class Scheduler:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def next_task(self, run: Run) -> Task | None:
        """First schedulable task for *run*, or ``None`` when none is pending.

        Ordering is (wave.sequence_order, task.seq) as returned by
        :func:`repo.list_tasks_for_run`.
        """
        for task in await repo.list_tasks_for_run(self.db, run.id):
            if task.status in _PICKABLE:
                return task
        return None

    async def pending_or_retry_count(self, run: Run) -> int:
        tasks = await repo.list_tasks_for_run(self.db, run.id)
        return sum(1 for t in tasks if t.status in _PICKABLE)

    async def all_terminal(self, run: Run) -> bool:
        tasks = await repo.list_tasks_for_run(self.db, run.id)
        return bool(tasks) and all(t.status in TERMINAL_TASK_STATUSES for t in tasks)

    async def all_completed(self, run: Run) -> bool:
        tasks = await repo.list_tasks_for_run(self.db, run.id)
        return bool(tasks) and all(t.status is TaskStatus.COMPLETED for t in tasks)
