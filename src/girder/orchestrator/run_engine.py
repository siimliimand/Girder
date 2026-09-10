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
active + pause steering                 "paused" (runs.paused flag set;
                                        steering still observed on next pumps)
paused + resume steering                falls through and resumes work
active + pending amendment              "awaiting_amendment"
active, task executed                   "task_completed" / "failed" /
                                        "integrity_violation"→"failed" /
                                        "amendment_pending"→"awaiting_amendment" /
                                        "budget_exhausted"
active, no tasks yet                    decompose, then proceed as above
active, wave task executed              "wave_executed" (Phase 4 wave path:
                                        tasks ran concurrently, integration
                                        pending)
active, wave integrated                 "wave_completed" (more waves) or
                                        completion as below
active, all tasks completed             "local_green" (no GitHub client) or
                                        "pr_opened" → delivery pump (Sprint 4):
                                        pr_open/ci_running/ci_fixing/
                                        conformance_review/merge_pending_human
                                        → "ci_pending" / "ci_green" /
                                        "ci_red_fixing" / "ci_fix_pushed" /
                                        "merge_pending_human" / "merged"
awaiting_amendment                      "awaiting_amendment"
github client missing in a delivery     "github_unavailable"
state
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
from collections.abc import Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from girder.config import Secrets, Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import (
    TERMINAL_ATTEMPT_STATUSES,
    TERMINAL_TASK_STATUSES,
    AttemptStatus,
    Project,
    Run,
    RunStatus,
    SteeringKind,
    Task,
    TaskStatus,
    Wave,
    WorktreeState,
)
from girder.fsm import InvalidTransition, transition_attempt, transition_run, transition_task
from girder.github.client import GitHubClient
from girder.gitops.branch import BranchOps
from girder.gitops.worktree import WorktreeManager
from girder.guard.redact import Redactor
from girder.models.gateway import ModelGateway
from girder.notify.notifier import Notifier
from girder.orchestrator.baseline import BaselineRunner
from girder.orchestrator.integrator import WaveIntegrator
from girder.orchestrator.scheduler import TERMINAL_WAVE_STATUSES, Scheduler, WavePlanError
from girder.orchestrator.task_engine import TaskEngine, TaskOutcome
from girder.sandbox.engine import SandboxEngine
from girder.specs.decomposer import DecompositionError, decompose_spec
from girder.specs.validator import SpecValidationError, parse_spec
from girder.util import run_host_cmd, utcnow_iso

log = logging.getLogger(__name__)

# Descriptors pump_once returns; callers treat all of these as pump exits.
STOP_DESCRIPTORS = frozenset(
    {
        "local_green",
        "github_unavailable",
        "ci_pending",
        "merge_pending_human",
        "merged",
        "failed",
        "aborted",
        "escalated",
        "budget_exhausted",
        "awaiting_amendment",
        "paused",
    }
)

# States the delivery engine (Sprint 4) takes over from local green onward.
DELIVERY_STATUSES = frozenset(
    {
        RunStatus.PR_OPEN,
        RunStatus.CI_RUNNING,
        RunStatus.CI_FIXING,
        RunStatus.CONFORMANCE_REVIEW,
        RunStatus.MERGE_PENDING_HUMAN,
    }
)

_PUMPABLE_RUN_STATUSES = frozenset(
    {
        RunStatus.SPEC_APPROVED,
        RunStatus.BASELINE_RUNNING,
        RunStatus.ACTIVE,
        *DELIVERY_STATUSES,
        # R14: `escalated` is terminal except an explicit operator abort —
        # POST /steer queues an abort in any status, so escalated runs stay
        # pumpable solely to consume that abort (impl-plan R14).
        RunStatus.ESCALATED,
        # §8.3: budget hard ceilings are per-cap, not per-run-lifetime; the
        # pump re-checks the (possibly raised) cap and resumes or re-parks.
        RunStatus.BUDGET_EXHAUSTED,
    }
)

_PROPOSAL_PATH = "openspec/proposals/{run_id}.md"

# Poll interval while an attempt is live and the pump watches for abort
# steering (plan.md Phase 5 task 2: abort must not wait for the next pump
# boundary).
_ABORT_POLL_S = 0.05


@dataclass
class _Inflight:
    """Live attempt asyncio.Tasks for one run, keyed by run id on the engine.

    The pump is single-threaded, but an attempt runs as its own Task so an
    abort observed mid-flight can cancel it immediately (with the container
    kill first) instead of letting it run to natural completion."""

    tasks: list[asyncio.Task[TaskOutcome]] = field(default_factory=list)


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
        github: GitHubClient | None = None,
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
        self._inflight: dict[str, _Inflight] = {}
        # Sprint 4: with a GitHub client the pump continues past local green
        # (PR → CI → conformance → merge per tier). Without one it parks at
        # the Sprint 3 terminal "local_green" (runs stay `active`).
        # Imported lazily: delivery -> task_engine -> orchestrator/__init__
        # -> run_engine would be circular at module import time.
        from girder.github.delivery import DeliveryEngine

        self.delivery: DeliveryEngine | None = (
            DeliveryEngine(
                db=db,
                settings=settings,
                secrets=secrets,
                gateway=gateway,
                sandbox=sandbox,
                notifier=notifier,
                redactor=redactor,
                github=github,
                repo_path=self._repo_path,
            )
            if github is not None
            else None
        )

    async def pump_once(self, run_id: str) -> str:
        run = await repo.get_run(self.db, run_id)
        if run is None:
            raise KeyError(f"run {run_id} not found")

        if run.status in (RunStatus.SPEC_APPROVED, RunStatus.BASELINE_RUNNING):
            return await self._pump_baseline(run)

        if run.status is RunStatus.ACTIVE:
            return await self._pump_active(run)

        if run.status in DELIVERY_STATUSES:
            if self.delivery is None:
                return "github_unavailable"
            return await self.delivery.pump(run)

        if run.status is RunStatus.AWAITING_AMENDMENT:
            return "awaiting_amendment"
        if run.status is RunStatus.ESCALATED:
            return await self._pump_escalated(run)
        if run.status is RunStatus.BUDGET_EXHAUSTED:
            return await self._pump_budget_exhausted(run)
        return str(run.status)

    async def _pump_escalated(self, run: Run) -> str:
        # R14: `escalated` is terminal except an explicit operator abort.
        # Consume abort steering only — other queued kinds stay unconsumed for
        # the post-review flow; without an abort the run parks unchanged
        # (escalation never auto-aborts).
        events = await repo.consume_steering_events(
            self.db, run.id, kinds=[SteeringKind.ABORT.value]
        )
        if not events:
            return "escalated"
        return await self._abort_run(run)

    async def _pump_budget_exhausted(self, run: Run) -> str:
        # §8.3: hard ceilings are per-cap, not per-run-lifetime — an operator
        # raising budget_cap_usd un-parks the run. The parked task is still
        # `running`, so the resumed ACTIVE pump picks it straight back up.
        if run.spend_usd >= run.budget_cap_usd:
            return "budget_exhausted"
        await transition_run(self.db, run.id, RunStatus.ACTIVE,
                            payload={"reason": "budget cap raised"})
        fresh = await repo.get_run(self.db, run.id)
        assert fresh is not None
        return await self._pump_active(fresh)

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
        base_commit = await self._base_commit(repo_path)
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
        # (a) steering: abort wins; pause/resume on the pump-level flag;
        # skip/force_pass apply immediately; inject stays unconsumed here —
        # the agent runtime absorbs it mid-turn, the task engine folds any
        # leftover into the next attempt's guidance.
        events = await repo.consume_steering_events(
            self.db,
            run.id,
            kinds=[
                SteeringKind.ABORT.value,
                SteeringKind.PAUSE.value,
                SteeringKind.RESUME.value,
                SteeringKind.SKIP.value,
                SteeringKind.FORCE_PASS.value,
            ],
        )
        if any(e["kind"] == SteeringKind.ABORT.value for e in events):
            # Clear the paused flag so aborted runs don't linger paused.
            await repo.set_run_paused(self.db, run.id, False)
            return await self._abort_run(run)

        # Skip/force_pass apply regardless of the paused flag.
        for event in events:
            if event["kind"] in (SteeringKind.SKIP.value, SteeringKind.FORCE_PASS.value):
                await self._apply_task_steering(run, event)

        had_pause = any(e["kind"] == SteeringKind.PAUSE.value for e in events)
        had_resume = any(e["kind"] == SteeringKind.RESUME.value for e in events)
        if not run.paused and had_pause:
            await repo.set_run_paused(self.db, run.id, True)
            await repo.insert_event(self.db, "steering_pause", {"run_id": run.id}, run_id=run.id)
            return "paused"
        if run.paused:
            if not had_resume:
                return "paused"  # pause while already paused is a no-op
            await repo.set_run_paused(self.db, run.id, False)
            await repo.insert_event(self.db, "steering_resume", {"run_id": run.id}, run_id=run.id)
            # Fall through: the same pump resumes work below.

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

        # (c2) Sprint 5: mechanical wave planning (idempotent). Runs with more
        # than one task go through the Phase 4 wave path — concurrent task
        # execution per wave, then serialized audit-gated integration.
        # Single-task runs keep the sequential path below.
        repo_path = await self._repo_path_of(run)
        try:
            waves = await self._scheduler.plan_run_waves(run, repo_path)
        except WavePlanError as exc:
            if self.notifier is not None:
                await self.notifier.notify(
                    "error", "Girder: wave planning failed", f"run {run.id}: {exc}",
                    run_id=run.id,
                )
            return await transition_run(self.db, run.id, RunStatus.FAILED,
                                        payload={"reason": str(exc)})
        if len(tasks) > 1:
            return await self._pump_wave(run, waves)

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
            if self.delivery is None:
                return "local_green"
            return await self.delivery.enter_delivery(fresh)
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
        results = await self._run_attempt_with_abort_watch(
            run, repo_path, engine.execute_task(run, task)
        )
        if results is None:
            return "aborted"  # abort steering observed mid-attempt
        first = results[0]
        if isinstance(first, BaseException):
            raise first  # preserve the old raise-through behavior
        outcome: TaskOutcome = first
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

    async def _apply_task_steering(self, run: Run, event: dict[str, Any]) -> None:
        """Apply one skip/force_pass steering event to its target task (Sprint 6).

        Never raises: unapplicable events are recorded as ``steering_ignored``;
        successful transitions as ``steering_applied``."""
        kind = str(event["kind"])
        new_state = (
            TaskStatus.SKIPPED if kind == SteeringKind.SKIP.value else TaskStatus.FORCE_PASSED
        )
        payload = event["payload"]
        task_id = str(payload.get("task_id", "")) if isinstance(payload, dict) else ""
        task = await repo.get_task(self.db, task_id) if task_id else None

        async def ignored(reason: str) -> None:
            await repo.insert_event(
                self.db, "steering_ignored", {"kind": kind, "reason": reason}, run_id=run.id
            )

        if task is None:
            await ignored(f"unknown task {task_id!r}")
            return
        row = await self.db.fetchone("SELECT run_id FROM waves WHERE id = ?", (task.wave_id,))
        if row is None or row["run_id"] != run.id:
            await ignored("task belongs to another run")
            return
        if task.status in TERMINAL_TASK_STATUSES:
            await ignored("task already terminal")
            return
        try:
            await transition_task(self.db, task.id, new_state, payload={"reason": "steering"})
        except InvalidTransition as exc:
            await ignored(str(exc))
            return
        await repo.insert_event(
            self.db,
            "steering_applied",
            {"kind": kind, "task_id": task.id},
            run_id=run.id,
        )

    # ------------------------------------------------- abort (Phase 5 task 2)

    async def _run_attempt_with_abort_watch(
        self,
        run: Run,
        repo_path: Path,
        *coros: Coroutine[Any, Any, TaskOutcome],
    ) -> list[TaskOutcome | BaseException] | None:
        """Run attempt coroutines as tracked asyncio.Tasks, racing an abort
        watcher so steering is observed *mid-attempt*, not at the next pump
        boundary (plan.md Phase 5 task 2).

        Returns the results list, or ``None`` when abort steering fired —
        :meth:`_abort_run` has then already performed the full teardown."""
        tasks = [asyncio.ensure_future(c) for c in coros]
        self._inflight[run.id] = _Inflight(tasks=tasks)
        watcher = asyncio.ensure_future(self._watch_abort(run.id))
        try:
            pending = {watcher, *tasks}
            while True:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                if watcher in done and not all(t.done() for t in tasks):
                    await self._abort_run(run, repo_path, tasks)
                    return None
                if not any(t in pending for t in tasks):
                    break
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            return list(await asyncio.gather(*tasks, return_exceptions=True))
        finally:
            self._inflight.pop(run.id, None)

    async def _watch_abort(self, run_id: str) -> None:
        """Poll for abort steering while attempts are live. Consuming here
        preserves the exactly-once delivery contract (the pump-boundary check
        simply sees no unconsumed abort afterwards)."""
        while True:
            events = await repo.consume_steering_events(
                self.db, run_id, kinds=[SteeringKind.ABORT.value]
            )
            if events:
                return
            await asyncio.sleep(_ABORT_POLL_S)

    async def _abort_run(
        self,
        run: Run,
        repo_path: Path | None = None,
        tasks: list[asyncio.Task[TaskOutcome]] | None = None,
    ) -> str:
        """Immediate abort teardown (plan.md Phase 5 task 2): kill live
        containers, cancel in-flight attempt tasks, transition those attempts
        to ``crashed``, prune the run's worktrees, reset the run branch to
        ``main``, then transition the run to ``ABORTED`` and notify."""
        repo_path = repo_path or await self._repo_path_of(run)
        live = await self._live_attempts(run.id)

        # 1. Kill the containers first — the sandbox must not outlive the
        #    steering decision even for the seconds an attempt might take to
        #    notice its own cancellation.
        for _attempt_id, container_id in live:
            if container_id:
                with suppress(Exception):
                    await self.sandbox.kill(container_id)

        # 2. Cancel the attempt tasks and let them settle. Cancellation
        #    protocol (impl-plan §6.12): task_engine's coroutine catches
        #    ``CancelledError``, kills its own container and re-raises with
        #    NO further DB status writes — the pump owns the transition below.
        if tasks is None:
            tasks = self._inflight.get(run.id, _Inflight()).tasks
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # 3. Attempt -> CRASHED. A steering race (attempt already terminal)
        #    keeps its status; it must never crash the pump.
        for attempt_id, _container_id in live:
            with suppress(InvalidTransition):
                await transition_attempt(
                    self.db,
                    attempt_id,
                    AttemptStatus.CRASHED,
                    payload={"reason": "abort steering"},
                )
            with suppress(Exception):
                await repo.update_attempt_fields(
                    self.db,
                    attempt_id,
                    failure_reason="aborted by operator steering",
                    ended_at=utcnow_iso(),
                )

        # 4. Prune every worktree the run still holds, then reset the branch.
        await self._prune_run_worktrees(run.id, repo_path)
        new_tip = await BranchOps(repo_path).reset_branch(run.branch, base_ref="main")
        if new_tip is None:
            log.warning(
                "run %s: branch %s not reset to main (missing ref or CAS loss)",
                run.id,
                run.branch,
            )
        await repo.insert_event(
            self.db,
            "run_aborted",
            {"branch_reset_to": new_tip, "crashed_attempts": [a for a, _ in live]},
            run_id=run.id,
        )
        if self.notifier is not None:
            await self.notifier.notify(
                "warning",
                "Girder: run aborted",
                f"run {run.id}: aborted by operator steering — containers killed, "
                f"branch {run.branch} reset to main",
                run_id=run.id,
            )
        return await transition_run(self.db, run.id, RunStatus.ABORTED,
                                    payload={"reason": "abort steering event"})

    async def _live_attempts(self, run_id: str) -> list[tuple[str, str | None]]:
        """Non-terminal attempts of *run* as (attempt_id, container_id)."""
        terminal = [s.value for s in TERMINAL_ATTEMPT_STATUSES]
        rows = await self.db.fetchall(
            f"""
            SELECT a.id AS attempt_id, a.container_id
            FROM attempts a
            JOIN tasks t ON a.task_id = t.id
            JOIN waves w ON t.wave_id = w.id
            WHERE w.run_id = ?
              AND a.status NOT IN ({','.join('?' for _ in terminal)})
            """,
            (run_id, *terminal),
        )
        return [(r["attempt_id"], r["container_id"]) for r in rows]

    async def _prune_run_worktrees(self, run_id: str, repo_path: Path) -> None:
        rows = await self.db.fetchall(
            """
            SELECT w.id AS worktree_id, w.path
            FROM worktrees w
            JOIN attempts a ON w.attempt_id = a.id
            JOIN tasks t ON a.task_id = t.id
            JOIN waves v ON t.wave_id = v.id
            WHERE v.run_id = ? AND w.state = 'active'
            """,
            (run_id,),
        )
        manager = WorktreeManager(repo_path)
        for r in rows:
            try:
                await manager.remove(r["path"], force=True)
            except Exception:
                log.exception("abort: failed to prune worktree %s", r["path"])
            with suppress(Exception):
                await repo.set_worktree_state(
                    self.db, r["worktree_id"], WorktreeState.PRUNED
                )

    # ------------------------------------------------------------------- waves

    async def _pump_wave(self, run: Run, waves: list[Wave]) -> str:
        """Phase 4 wave path: execute one wave's tasks concurrently, then
        integrate them one by one (audit + full suite per merge)."""
        wave = next((w for w in waves if w.status not in TERMINAL_WAVE_STATUSES), None)
        if wave is None:
            return await self._wave_completion(run)

        if wave.status == "pending":
            await repo.set_wave_status(self.db, wave.id, "running")
            await repo.insert_event(
                self.db,
                "wave_started",
                {"wave_id": wave.id, "sequence_order": wave.sequence_order},
                run_id=run.id,
            )

        tasks = await repo.list_tasks_for_wave(self.db, wave.id)
        # `running` here = an amendment resume or crash-recovery re-entry;
        # verify_passed tasks belong to the integrator, not the agents.
        actionable = [
            t for t in tasks
            if t.status in (TaskStatus.PENDING, TaskStatus.RETRY_SCHEDULED, TaskStatus.RUNNING)
        ]
        if actionable:
            return await self._execute_wave(run, wave, actionable)
        return await self._integrate_wave(run, wave)

    async def _execute_wave(
        self, run: Run, wave: Wave, actionable: list[Task]
    ) -> str:
        """All actionable tasks of the wave concurrently — each in its own
        worktree and container (plan.md Phase 4 task 3, SC-14)."""
        project = await self._project(run)
        repo_path = await self._repo_path_of(run)
        engines = [
            TaskEngine(
                db=self.db,
                gateway=self.gateway,
                sandbox=self.sandbox,
                settings=self.settings,
                redactor=self.redactor,
                notifier=self.notifier,
                project=project,
                repo_path=repo_path,
            )
            for _ in actionable
        ]
        results = await self._run_attempt_with_abort_watch(
            run,
            repo_path,
            *(
                engine.execute_task(run, task, integrate=False)
                for engine, task in zip(engines, actionable, strict=True)
            ),
        )
        if results is None:
            return "aborted"  # abort steering observed mid-wave
        crashes = [r for r in results if isinstance(r, BaseException)]
        if crashes:
            # Every engine has settled; re-raise the first programming error.
            raise crashes[0]
        outcomes: list[TaskOutcome] = []
        for r in results:
            assert isinstance(r, TaskOutcome)  # narrowed: no exceptions above
            outcomes.append(r)
        for task, outcome in zip(actionable, outcomes, strict=True):
            log.info(
                "run %s wave %s task %s -> %s",
                run.id, wave.sequence_order, task.id, outcome.kind,
            )
        kinds = {o.kind for o in outcomes}
        if "failed" in kinds or "integrity_violation" in kinds:
            offending = next(o for o in outcomes if o.kind in ("failed", "integrity_violation"))
            fresh = await repo.get_run(self.db, run.id)
            assert fresh is not None
            if self.notifier is not None:
                await self.notifier.notify(
                    "error",
                    "Girder: run failed",
                    f"run {fresh.id}: wave task failed ({offending.detail or offending.kind})",
                    run_id=fresh.id,
                )
            return await transition_run(self.db, fresh.id, RunStatus.FAILED,
                                        payload={"reason": offending.detail or offending.kind})
        if "budget_exhausted" in kinds:
            return "budget_exhausted"
        if "amendment_pending" in kinds:
            return "awaiting_amendment"
        return "wave_executed"

    async def _integrate_wave(self, run: Run, wave: Wave) -> str:
        project = await self._project(run)
        repo_path = await self._repo_path_of(run)
        integrator = WaveIntegrator(
            db=self.db,
            gateway=self.gateway,
            sandbox=self.sandbox,
            settings=self.settings,
            redactor=self.redactor,
            notifier=self.notifier,
            project=project,
            repo_path=repo_path,
        )
        outcome = await integrator.integrate_wave(run, wave)
        log.info("run %s wave %s integration -> %s", run.id, wave.sequence_order, outcome.kind)
        if outcome.kind == "wave_completed":
            await repo.set_wave_status(self.db, wave.id, "completed")
            return await self._wave_completion(run)
        if outcome.kind == "budget_exhausted":
            return "budget_exhausted"
        if outcome.kind == "escalated":
            fresh = await repo.get_run(self.db, run.id)
            assert fresh is not None
            reason = outcome.detail or "wave integration escalated"
            return await transition_run(self.db, fresh.id, RunStatus.ESCALATED,
                                        payload={"reason": reason})
        fresh = await repo.get_run(self.db, run.id)
        assert fresh is not None
        if self.notifier is not None:
            await self.notifier.notify(
                "error",
                "Girder: run failed",
                f"run {fresh.id}: wave integration failed ({outcome.detail})",
                run_id=fresh.id,
            )
        return await transition_run(self.db, fresh.id, RunStatus.FAILED,
                                    payload={"reason": outcome.detail or "wave integration failed"})

    async def _wave_completion(self, run: Run) -> str:
        """After a wave completes: next wave, or local green / delivery."""
        waves = await repo.list_waves_for_run(self.db, run.id)
        if any(w.status not in TERMINAL_WAVE_STATUSES for w in waves):
            return "wave_completed"
        fresh = await repo.get_run(self.db, run.id)
        assert fresh is not None
        if await self._scheduler.all_completed(fresh):
            await self._announce_local_green(fresh)
            if self.delivery is None:
                return "local_green"
            return await self.delivery.enter_delivery(fresh)
        reason = "waves finished but not all tasks completed"
        if self.notifier is not None:
            await self.notifier.notify(
                "error", "Girder: run failed", f"run {fresh.id}: {reason}", run_id=fresh.id
            )
        return await transition_run(self.db, fresh.id, RunStatus.FAILED,
                                    payload={"reason": reason})

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

    async def _base_commit(self, repo_path: Path) -> str:
        """Anchor the baseline at ``main`` (plan.md Phase 2 task 1) — the
        project checkout's HEAD may sit on any branch. Falls back to
        ``origin/main``, then to HEAD with a warning when neither exists."""
        branch_ops = BranchOps(repo_path)
        for ref in ("main", "origin/main"):
            sha = await branch_ops.resolve_ref(ref)
            if sha is not None:
                return sha
        log.warning(
            "no `main` or `origin/main` in %s — anchoring baseline at HEAD", repo_path
        )
        return await self._head_commit(repo_path)

    async def _head_commit(self, repo_path: Path) -> str:
        proc = await run_host_cmd(["git", "-C", str(repo_path), "rev-parse", "HEAD"], timeout_s=30)
        return proc.stdout.strip()
