"""Single isolated Diagnostic Fix Agent (D5) — impl-plan §6.11, plan.md Phase 3 task 5.

When CI is red, exactly one fix agent runs at a time: each fix is a real
``fix``-type Task row executed through the SAME
:class:`~girder.orchestrator.task_engine.TaskEngine` verification loop as
ordinary tasks — full suite, Layer-2 test-manifest re-hash, Layer-3 DiffAudit,
audit-gated ff-only merge onto the run branch. A fix that would violate
integrity (test tampering, protected paths) cannot land, exactly like any
other task; ``scope_globs=["**"]`` only relaxes ordinary code-path scoping,
which a CI fix legitimately needs (TaskType.FIX keeps full Layer 1-3 test
protection per impl-plan §4.2).

The fix-attempt cap (:class:`girder.config.LimitsConfig.ci_fix_attempts`) is
counted from persisted Task rows titled ``fix: …``, so it survives crashes.
This module never transitions run FSM state; the delivery engine owns runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from girder.config import Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run, TaskType
from girder.guard.redact import Redactor
from girder.models.gateway import ModelGateway
from girder.notify.notifier import Notifier
from girder.orchestrator.task_engine import TaskEngine
from girder.sandbox.engine import SandboxEngine

log = logging.getLogger(__name__)

FIX_TITLE_PREFIX = "fix: "
_FAILURE_REPORT_MAX_CHARS = 4000

# TaskOutcome.kind -> DiagnosticOutcome.kind (1:1; integrity violations and
# ordinary failures both surface as "failed" — the cap and the delivery
# engine decide what happens next).
_OUTCOME_KINDS = {
    "completed": "fixed",
    "failed": "failed",
    "integrity_violation": "failed",
    "amendment_pending": "amendment_pending",
    "budget_exhausted": "budget_exhausted",
}


@dataclass(frozen=True)
class DiagnosticOutcome:
    kind: str  # "fixed" | "failed" | "budget_exhausted" | "amendment_pending"
    detail: str | None = None


async def count_fix_tasks(db: Database, run_id: str) -> int:
    """Number of tasks for *run_id* whose title starts with ``fix: ``.

    The persistent cap counter — derived from the DB on every call so it
    survives crashes and restarts; the delivery engine checks it against
    ``settings.limits.ci_fix_attempts``.
    """
    rows = await db.fetchall(
        "SELECT COUNT(*) AS n FROM tasks t JOIN waves w ON t.wave_id = w.id"
        " WHERE w.run_id = ? AND t.title LIKE ?",
        (run_id, FIX_TITLE_PREFIX + "%"),
    )
    return int(rows[0]["n"]) if rows else 0


class DiagnosticEngine:
    """One fix agent at a time; every fix is a first-class Task row."""

    def __init__(
        self,
        *,
        db: Database,
        gateway: ModelGateway,
        sandbox: SandboxEngine,
        settings: Settings,
        redactor: Redactor,
        notifier: Notifier | None,
        project: Project,
        repo_path: Path,
        worktree_base: Path | None = None,
    ) -> None:
        self.db = db
        self.gateway = gateway
        self.sandbox = sandbox
        self.settings = settings
        self.redactor = redactor
        self.notifier = notifier
        self.project = project
        self.repo_path = Path(repo_path)
        self.worktree_base = worktree_base

    async def run(self, *, run: Run, failure_report: str) -> DiagnosticOutcome:
        """Create and execute one ``fix`` task from the (redacted) CI failure
        report, or refuse when the fix-attempt budget is exhausted."""
        used = await count_fix_tasks(self.db, run.id)
        if used >= self.settings.limits.ci_fix_attempts:
            log.warning("run %s: ci fix attempts exhausted (%d)", run.id, used)
            return DiagnosticOutcome("failed", "ci fix attempts exhausted")

        tasks = await repo.list_tasks_for_run(self.db, run.id)
        wave = await repo.get_or_create_wave0(self.db, run.id)
        seq = len(tasks)
        redacted_report, _ = self.redactor.redact(failure_report[:_FAILURE_REPORT_MAX_CHARS])
        task = await repo.create_task(
            self.db,
            wave.id,
            seq,
            f"{FIX_TITLE_PREFIX}CI failures ({used + 1})",
            TaskType.FIX,
            scope_globs=["**"],
            spec_slice_md=redacted_report,
        )
        log.info("run %s: fix task %s created (attempt %d)", run.id, task.id, used + 1)

        fresh_run = await repo.get_run(self.db, run.id) or run
        engine = TaskEngine(
            db=self.db,
            gateway=self.gateway,
            sandbox=self.sandbox,
            settings=self.settings,
            redactor=self.redactor,
            notifier=self.notifier,
            project=self.project,
            repo_path=self.repo_path,
            worktree_base=self.worktree_base,
        )
        outcome = await engine.execute_task(fresh_run, task)
        kind = _OUTCOME_KINDS.get(outcome.kind, "failed")
        await repo.insert_event(
            self.db,
            "ci_fix_attempted",
            {"task_id": task.id, "outcome": outcome.kind},
            run_id=run.id,
        )
        return DiagnosticOutcome(kind, outcome.detail)
