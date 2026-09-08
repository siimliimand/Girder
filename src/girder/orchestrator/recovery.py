"""Boot-time crash recovery (plan.md §8.7 / impl-plan §6.12).

The SQLite state store is the single source of truth; on boot, *after*
migrations, this service reconciles the world to match it:

1. Any attempt left ``running`` is transitioned to ``crashed`` — an attempt is
   disposable (D8); completed tasks are never re-executed.
2. Worktrees of crashed/terminal attempts are pruned (``git worktree remove
   --force``); unremovable ones are quarantined for human inspection.
3. Tasks caught mid-flight (``running`` / ``verifying``) are rescheduled — or
   marked failed outright when the attempt budget is exhausted.
4. If anything was recovered, a notification with a recovery report goes out
   (through the redaction pipeline, like every notification).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from girder.config import Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import (
    TERMINAL_RUN_STATUSES,
    AttemptStatus,
    TaskStatus,
    WorktreeState,
)
from girder.fsm import transition_attempt, transition_task
from girder.gitops.worktree import WorktreeManager
from girder.notify.notifier import Notifier

log = logging.getLogger(__name__)


@dataclass
class RecoveryReport:
    crashed_attempts: list[str] = field(default_factory=list)
    pruned_worktrees: list[str] = field(default_factory=list)
    quarantined_worktrees: list[str] = field(default_factory=list)
    rescheduled_tasks: list[str] = field(default_factory=list)
    failed_tasks: list[str] = field(default_factory=list)

    @property
    def anything_recovered(self) -> bool:
        return bool(
            self.crashed_attempts
            or self.pruned_worktrees
            or self.quarantined_worktrees
            or self.rescheduled_tasks
            or self.failed_tasks
        )

    def summary(self) -> str:
        lines = ["Recovery report:"]
        if self.crashed_attempts:
            lines.append(f"- {len(self.crashed_attempts)} attempt(s) crashed mid-flight")
        if self.pruned_worktrees:
            lines.append(f"- {len(self.pruned_worktrees)} worktree(s) pruned")
        if self.quarantined_worktrees:
            lines.append(f"- {len(self.quarantined_worktrees)} worktree(s) quarantined")
        if self.rescheduled_tasks:
            lines.append(f"- {len(self.rescheduled_tasks)} task(s) rescheduled for retry")
        if self.failed_tasks:
            lines.append(f"- {len(self.failed_tasks)} task(s) failed (attempt budget exhausted)")
        if not self.anything_recovered:
            lines.append("- nothing to recover; state was clean")
        return "\n".join(lines)


class RecoveryService:
    def __init__(self, db: Database, settings: Settings, notifier: Notifier | None = None):
        self.db = db
        self.settings = settings
        self.notifier = notifier

    async def recover(self) -> RecoveryReport:
        report = RecoveryReport()
        await self._crash_running_attempts(report)
        await self._reschedule_interrupted_tasks(report)
        await self._prune_dead_worktrees(report)
        await self._verify_no_live_worktrees_on_terminal_runs()
        if report.anything_recovered and self.notifier is not None:
            await self.notifier.notify("warning", "Girder restarted after crash", report.summary())
        log.info("%s", report.summary())
        return report

    # ------------------------------------------------------------------ steps

    async def _crash_running_attempts(self, report: RecoveryReport) -> None:
        for attempt in await repo.find_attempts_in_status(self.db, AttemptStatus.RUNNING):
            await transition_attempt(
                self.db, attempt.id, AttemptStatus.CRASHED, payload={"reason": "host restart"}
            )
            await repo.update_attempt_fields(
                self.db, attempt.id, failure_reason="crashed: orchestrator restart"
            )
            report.crashed_attempts.append(attempt.id)

    async def _reschedule_interrupted_tasks(self, report: RecoveryReport) -> None:
        interrupted = await repo.find_tasks_in_statuses(
            self.db, [TaskStatus.RUNNING.value, TaskStatus.VERIFYING.value]
        )
        for task in interrupted:
            if task.attempts_used >= self.settings.limits.task_max_attempts:
                await transition_task(
                    self.db,
                    task.id,
                    TaskStatus.FAILED,
                    payload={"reason": "attempt budget exhausted at restart"},
                )
                report.failed_tasks.append(task.id)
            else:
                await transition_task(
                    self.db,
                    task.id,
                    TaskStatus.RETRY_SCHEDULED,
                    payload={"reason": "recovered after restart"},
                )
                report.rescheduled_tasks.append(task.id)

    async def _prune_dead_worktrees(self, report: RecoveryReport) -> None:
        """Remove worktrees whose attempt is no longer live (crashed/terminal)."""
        for ctx in await repo.find_active_worktrees_with_context(self.db):
            if ctx["status"] == AttemptStatus.RUNNING.value:
                continue  # still live — nothing to do (should not happen post-step-1)
            manager = WorktreeManager(Path(ctx["repo_path"]))
            try:
                await manager.remove(ctx["path"], force=True)
                await repo.set_worktree_state(self.db, ctx["worktree_id"], WorktreeState.PRUNED)
                report.pruned_worktrees.append(ctx["path"])
            except Exception:
                await repo.set_worktree_state(
                    self.db, ctx["worktree_id"], WorktreeState.QUARANTINED
                )
                report.quarantined_worktrees.append(ctx["path"])
                log.exception("could not prune worktree %s", ctx["path"])

    async def _verify_no_live_worktrees_on_terminal_runs(self) -> None:
        """Sanity probe: no *active* worktree row may belong to a terminal run.

        The GC daemon would eventually collect these; recovery flags them now
        so a reboot surfaces the inconsistency immediately.
        """
        terminal = {s.value for s in TERMINAL_RUN_STATUSES}
        for ctx in await repo.find_active_worktrees_with_context(self.db):
            if ctx["run_status"] in terminal:
                log.warning(
                    "active worktree %s belongs to terminal run (status=%s) — GC will collect it",
                    ctx["path"],
                    ctx["run_status"],
                )
