"""The run-level FSM pump (impl-plan §6.12, plan.md Phase 2 exit criteria).

``pump_once`` performs at most one state-level action for a run and returns a
descriptor string the caller (daemon poll loop, ``girder pump``, tests) can
branch on:

======================================  =====================================
run status / situation                  descriptor
======================================  =====================================
spec_approved / baseline_running        "active" (baseline green)
spec_approved / baseline_running        "escalated" (baseline broken/infra)
active + abort steering                 "aborted"
active + pause steering                 "paused"
active + pending amendment              "awaiting_amendment"
active, task executed                   "task_completed" / "failed" /
                                        "integrity_violation"→"failed" /
                                        "amendment_pending"→"awaiting_amendment" /
                                        "budget_exhausted"
active, no tasks yet                    decompose, then proceed as above
active, all tasks completed             "local_green" (Sprint 3 terminal;
                                        run stays ``active`` for Sprint 4 PR)
awaiting_amendment                      "awaiting_amendment"
budget_exhausted / escalated / terminal status string as-is
======================================  =====================================

Amendment resolution happens out-of-band (web/API): ``resolve_amendment``
flips the run back to ``active`` and the task to ``running``; the next pump
resumes the amended task (the scheduler deliberately skips ``running`` tasks
that are mid-flight, but a ``running`` task observed by the pump after an
amendment resolution has no live attempt — the pump is single-threaded).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from girder.config import Secrets, Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run, RunStatus, SteeringKind, Task, TaskStatus
from girder.fsm import transition_run
from girder.gitops.branch import BranchOps
from girder.guard.redact import Redactor
from girder.models.gateway import ModelGateway
from girder.notify.notifier import Notifier
from girder.orchestrator.baseline import BaselineRunner
from girder.orchestrator.scheduler import Scheduler
from girder.orchestrator.task_engine import TaskEngine, TaskOutcome
from girder.sandbox.engine import SandboxEngine
from girder.specs.decomposer import DecompositionError, decompose_spec
from girder.specs.validator import SpecValidationError, parse_spec
from girder.util import run_host_cmd

log = logging.getLogger(__name__)

# Descriptors pump_once returns; callers treat all of these as pump exits.
STOP_DESCRIPTORS = frozenset(
    {
        "local_green",
        "failed",
        "aborted",
        "escalated",
        "budget_exhausted",
        "awaiting_amendment",
        "paused",
    }
)

_PUMPABLE_RUN_STATUSES = frozenset(
    {RunStatus.SPEC_APPROVED, RunStatus.BASELINE_RUNNING, RunStatus.ACTIVE}
)

_PROPOSAL_PATH = "openspec/proposals/{run_id}.md"


class RunEngine:
    def __init__(
        self,
        *,
        db: Database,
        settings: Settings,
        secrets: Secrets,
        gateway: ModelGateway,
        sandbox: SandboxEngine,
        notifier: Notifier | None,
        redactor: Redactor,
        repo_path: Path | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.secrets = secrets
        self.gateway = gateway
        self.sandbox = sandbox
        self.notifier = notifier
        self.redactor = redactor
        self._repo_path = Path(repo_path) if repo_path else None
        self._scheduler = Scheduler(db)

    async def pump_once(self, run_id: str) -> str:
        run = await repo.get_run(self.db, run_id)
        if run is None:
            raise KeyError(f"run {run_id} not found")

        if run.status in (RunStatus.SPEC_APPROVED, RunStatus.BASELINE_RUNNING):
            return await self._pump_baseline(run)

        if run.status is RunStatus.ACTIVE:
            return await self._pump_active(run)

        if run.status is RunStatus.AWAITING_AMENDMENT:
            return "awaiting_amendment"
        return str(run.status)

    async def run_to_completion(
        self, run_id: str, *, poll_s: float = 2.0, max_pumps: int = 500
    ) -> str:
        """Pump until a stop descriptor; ``poll_s`` is unused today (all pumps
        are local work) but kept for the future remote-action surface."""
        descriptor = ""
        for _ in range(max_pumps):
            descriptor = await self.pump_once(run_id)
            if descriptor in STOP_DESCRIPTORS:
                return descriptor
            await asyncio.sleep(poll_s if descriptor == "" else 0)
        return descriptor

    # --------------------------------------------------------------- baseline

    async def _pump_baseline(self, run: Run) -> str:
        project = await self._project(run)
        repo_path = await self._repo_path_of(run)
        if run.status is RunStatus.SPEC_APPROVED:
            await transition_run(self.db, run.id, RunStatus.BASELINE_RUNNING)
        base_commit = await self._head_commit(repo_path)
        branch_ops = BranchOps(repo_path)
        await branch_ops.ensure_run_branch(run.branch, base_commit=base_commit)

        runner = BaselineRunner(db=self.db, sandbox=self.sandbox, settings=self.settings,
                                redactor=self.redactor)
        outcome = await runner.run(project=project, run=run, repo_path=repo_path,
                                   base_commit=base_commit)
        if outcome.baseline_run_id:
            await repo.update_run_fields(self.db, run.id, baseline_run_id=outcome.baseline_run_id)
        if outcome.broken or outcome.infra_error:
            reason = outcome.infra_error or f"broken baseline: {outcome.broken_ids}"
            if self.notifier is not None:
                await self.notifier.notify(
                    "error",
                    "Girder: baseline broken — run escalated",
                    f"run {run.id}: {reason}",
                    run_id=run.id,
                )
            return await transition_run(self.db, run.id, RunStatus.ESCALATED,
                                        payload={"reason": reason})
        return await transition_run(self.db, run.id, RunStatus.ACTIVE)

    # ----------------------------------------------------------------- active

    async def _pump_active(self, run: Run) -> str:
        # (a) steering: abort wins; pause parks the pump; the rest is Sprint 6.
        events = await repo.consume_steering_events(self.db, run.id)
        aborts = [e for e in events if e["kind"] == SteeringKind.ABORT.value]
        if aborts:
            return await transition_run(self.db, run.id, RunStatus.ABORTED,
                                        payload={"reason": "abort steering event"})
        pauses = [e for e in events if e["kind"] == SteeringKind.PAUSE.value]
        if pauses:
            await repo.insert_event(self.db, "steering_pause", {"run_id": run.id}, run_id=run.id)
            return "paused"
        deferred = [
            e for e in events
            if e["kind"] in (SteeringKind.INJECT.value, SteeringKind.SKIP.value,
                             SteeringKind.FORCE_PASS.value)
        ]
        for e in deferred:
            await repo.insert_event(
                self.db, "steering_deferred", {"kind": e["kind"], "payload": e["payload"]},
                run_id=run.id,
            )

        # (b) a pending amendment parks the run until resolved via the API.
        if await repo.get_pending_amendment(self.db, run.id) is not None:
            return "awaiting_amendment"

        # (c) no tasks yet: read the frozen proposal from the run branch and
        # decompose it exactly once.
        tasks = await repo.list_tasks_for_run(self.db, run.id)
        if not tasks:
            fresh = await repo.get_run(self.db, run.id)
            assert fresh is not None
            if not await self._decompose(fresh):
                return "failed"
            tasks = await repo.list_tasks_for_run(self.db, run.id)

        # (d) execute the next task; resume a `running` task left parked by an
        # approved amendment (single-threaded pump: nothing is mid-flight).
        task = await self._scheduler.next_task(run)
        if task is None:
            for candidate in tasks:
                if candidate.status is TaskStatus.RUNNING:
                    task = candidate
                    break
        if task is not None:
            return await self._execute(run, task)

        fresh = await repo.get_run(self.db, run.id)
        assert fresh is not None
        if await self._scheduler.all_completed(fresh):
            await self._announce_local_green(fresh)
            return "local_green"
        # Tasks exist but none schedulable and not all completed: dropped or
        # leaked task — the run cannot proceed.
        reason = "no schedulable task and not all tasks completed"
        if self.notifier is not None:
            await self.notifier.notify(
                "error", "Girder: run failed", f"run {fresh.id}: {reason}", run_id=fresh.id
            )
        return await transition_run(self.db, fresh.id, RunStatus.FAILED,
                                    payload={"reason": reason})

    async def _execute(self, run: Run, task: Task) -> str:
        project = await self._project(run)
        repo_path = await self._repo_path_of(run)
        engine = TaskEngine(
            db=self.db,
            gateway=self.gateway,
            sandbox=self.sandbox,
            settings=self.settings,
            redactor=self.redactor,
            notifier=self.notifier,
            project=project,
            repo_path=repo_path,
        )
        outcome: TaskOutcome = await engine.execute_task(run, task)
        log.info("run %s task %s -> %s", run.id, task.id, outcome.kind)
        if outcome.kind == "completed":
            return "task_completed"
        if outcome.kind == "amendment_pending":
            return "awaiting_amendment"
        if outcome.kind == "budget_exhausted":
            return "budget_exhausted"
        # failed | integrity_violation — the run cannot continue (§4.3).
        fresh = await repo.get_run(self.db, run.id)
        assert fresh is not None
        if self.notifier is not None:
            await self.notifier.notify(
                "error",
                "Girder: run failed",
                f"run {fresh.id} task {task.id}: {outcome.kind} ({outcome.detail})",
                run_id=fresh.id,
            )
        return await transition_run(self.db, fresh.id, RunStatus.FAILED,
                                    payload={"reason": outcome.detail or outcome.kind})

    # --------------------------------------------------------------- internals

    async def _decompose(self, run: Run) -> bool:
        """Read the frozen proposal off the run branch and decompose it."""
        repo_path = await self._repo_path_of(run)
        path = _PROPOSAL_PATH.format(run_id=run.id)
        proc = await run_host_cmd(
            ["git", "-C", str(repo_path), "show", f"{run.branch}:{path}"],
            check=False,
            timeout_s=30,
        )
        try:
            if proc.returncode != 0:
                raise SpecValidationError([f"frozen proposal {path} missing on {run.branch}"])
            spec = parse_spec(proc.stdout)
            await decompose_spec(self.db, run, spec)
            return True
        except (SpecValidationError, DecompositionError) as exc:
            if self.notifier is not None:
                await self.notifier.notify(
                    "error", "Girder: decomposition failed", f"run {run.id}: {exc}", run_id=run.id
                )
            await transition_run(self.db, run.id, RunStatus.FAILED,
                                 payload={"reason": str(exc)})
            return False

    async def _announce_local_green(self, run: Run) -> None:
        # Idempotent: the daemon re-pumps active runs forever after local green.
        already = await repo.get_latest_event(self.db, run.id, "run_local_green")
        if already is not None:
            return
        tasks = await repo.list_tasks_for_run(self.db, run.id)
        usage = await repo.list_token_usage_for_run(self.db, run.id)
        total_cost = sum(float(u["cost_usd"]) for u in usage)
        payload = {
            "task_titles": [t.title for t in tasks],
            "task_count": len(tasks),
            "total_cost_usd": round(total_cost, 6),
        }
        await repo.insert_event(self.db, "run_local_green", payload, run_id=run.id)
        if self.notifier is not None:
            lines = "\n".join(f"- {t.title}" for t in tasks)
            await self.notifier.notify(
                "info",
                "Girder: local green",
                f"run {run.id} completed all {len(tasks)} task(s), total cost "
                f"${total_cost:.4f}\n{lines}",
                run_id=run.id,
            )

    async def _project(self, run: Run) -> Project:
        project = await repo.get_project(self.db, run.project_id)
        if project is None:
            raise KeyError(f"project {run.project_id} not found")
        return project

    async def _repo_path_of(self, run: Run) -> Path:
        if self._repo_path is not None:
            return self._repo_path
        project = await self._project(run)
        return Path(project.repo_path)

    async def _head_commit(self, repo_path: Path) -> str:
        proc = await run_host_cmd(["git", "-C", str(repo_path), "rev-parse", "HEAD"], timeout_s=30)
        return proc.stdout.strip()
