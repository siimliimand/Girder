"""Task engine: retry loop driving one task to a terminal outcome.

``execute_task`` drives up to ``task_max_attempts`` sequential attempts
(§5.5 — park state first, act after): park the task FSM, allocate the
attempt, capture the Layer-2 test manifest anchor, run the agent turn loop
in the sandbox, then verify: suite in the same container, kill it (D10:
quiesced tree before the audit), and only a green Layer 2/3 diff audit
reaches the ff-only audit-gated merge. Integrity violations (§8.2) fail
WITHOUT retry and never merge; budget exceptions surface as
``budget_exhausted``.

Module layout (WP 10.2): allocation/teardown in
``attempt_lifecycle``, verify/integrate in ``verification``, dirty-worktree
salvage in ``work_salvage``; every pre-split public name is re-exported
here, so this module remains the only import surface.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from girder.budget.guard import BudgetExceeded
from girder.config import Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import (
    Project,
    Run,
    Task,
    TaskStatus,
)
from girder.fsm import transition_task
from girder.gitops.audit import DiffAudit
from girder.gitops.branch import BranchOps
from girder.guard.redact import Redactor
from girder.index.inject import codebase_index_guidance
from girder.models.gateway import ModelGateway
from girder.notify.notifier import Notifier
from girder.orchestrator.attempt_lifecycle import (
    AttemptContext,
    allocate_attempt,
    build_runtime,
    close_attempt,
    fail_task,
    record_engine_error,
    retry_or_fail,
    standing_guidance,
    teardown_attempt,
)
from girder.orchestrator.verification import (
    _VERIFY_TIMEOUT_S,
    VERIFY_CMD,
    VerificationResult,
    fail_without_retry,
    redact_tail,
    run_verification,
)
from girder.orchestrator.work_salvage import (
    salvage_dirty_worktree,
    salvage_guidance,
    salvage_worktree,
)
from girder.sandbox.engine import ContainerSpec, SandboxEngine
from girder.specs import amendment as spec_amendment
from girder.stacks import STACK_REGISTRY, VERIFY_XML  # noqa: F401  (re-export)

# Backward-compat re-exports: this module remains the only import surface
# for the split-out pieces (zero changes to existing call sites).
__all__ = [
    "VERIFY_CMD",
    "VERIFY_XML",
    "_VERIFY_TIMEOUT_S",
    "AttemptContext",
    "TaskEngine",
    "TaskOutcome",
    "VerificationResult",
    "allocate_attempt",
    "run_verification",
    "salvage_dirty_worktree",
    "teardown_attempt",
]


@dataclass(frozen=True)
class TaskOutcome:
    # completed | verified (wave mode, integration deferred) | failed |
    # integrity_violation | amendment_pending | budget_exhausted
    kind: str
    detail: str | None = None
    # True when the agent declared mark_task_complete(no_changes=true) and the
    # empty diff was accepted as a legitimate no-op (Group D).
    no_changes: bool = False


def _kind_of(status: TaskStatus) -> str:
    return {
        TaskStatus.COMPLETED: "completed",
        TaskStatus.FAILED: "failed",
        TaskStatus.AWAITING_AMENDMENT: "amendment_pending",
    }.get(status, "failed")


class TaskEngine:
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
        image: str | None = None,
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
        self.image = image or f"girder-runner:{settings.project.stack}"
        self._audit = DiffAudit(
            test_signal_patterns=settings.project.test_signal_patterns,
            protected_read_paths=settings.project.protected_read_paths,
        )
        self._snapshot_dirs: dict[str, list[Path]] = {}
        # Group B: task_id → salvage commit sha from the most recent failed
        # attempt; consumed by allocate_attempt to base the retry worktree
        # on the salvaged task-branch tip.
        self._salvage_tips: dict[str, str] = {}

    def _container_spec(self, name: str, worktree: Path) -> ContainerSpec:
        """Build the attempt ContainerSpec (impl-plan §6.4, R11), forwarding
        the operator's ``[sandbox]`` network/resource policy. Kept as a
        method: tests call it directly."""
        return ContainerSpec(
            name=name,
            image=self.image,
            worktree=worktree,
            network=self.settings.sandbox.network,
            memory=self.settings.sandbox.memory,
            cpus=self.settings.sandbox.cpus,
            pids_limit=self.settings.sandbox.pids_limit,
        )

    def _redact_tail(self, text: str) -> str:
        # Compatibility shim: verification.redact_tail is the real impl.
        return redact_tail(self, text)

    async def execute_task(self, run: Run, task: Task, *, integrate: bool = True) -> TaskOutcome:
        """Run *task* to a terminal outcome (or an amendment park).

        With ``integrate=False`` (Sprint 5 wave mode) a green, audit-clean
        attempt stops at ``verify_passed``; the wave integrator merges
        verified task branches one by one (plan.md Phase 4 task 4).
        """
        max_attempts = self.settings.limits.task_max_attempts
        steering_guidance = await standing_guidance(self, run, task)
        guidance: str | None = None
        branch_ops = BranchOps(self.repo_path)

        def _effective_guidance() -> str | None:
            if steering_guidance is None:
                return guidance
            if guidance is None:
                return steering_guidance
            return f"{steering_guidance}\n{guidance}"

        while True:
            fresh = await repo.get_task(self.db, task.id)
            if fresh is None:
                raise KeyError(f"task {task.id} not found")
            if fresh.status is TaskStatus.VERIFY_PASSED:
                # Wave mode: already verified, waiting on the integrator.
                return TaskOutcome("verified")
            if fresh.status in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.AWAITING_AMENDMENT,
            ):
                return TaskOutcome(_kind_of(fresh.status))
            if fresh.attempts_used >= max_attempts:
                await fail_task(self, fresh, "attempt budget exhausted")
                return TaskOutcome("failed", "attempt budget exhausted")

            # 1. Park task state first (§5.5). A task already `running` is an
            # amendment resume — it was parked before the amendment request.
            state: TaskStatus = fresh.status
            if state is TaskStatus.PENDING:
                state = TaskStatus(
                    await transition_task(self.db, fresh.id, TaskStatus.SCHEDULED)
                )
            if state is TaskStatus.SCHEDULED:
                await transition_task(self.db, fresh.id, TaskStatus.RUNNING)
            elif state is TaskStatus.RETRY_SCHEDULED:
                await transition_task(self.db, fresh.id, TaskStatus.RUNNING)

            # 2. Base commit = current run-branch tip.
            base_commit = await branch_ops.run_branch_tip(run.branch)
            if base_commit is None:
                await fail_task(self, fresh, f"run branch {run.branch} has no tip")
                return TaskOutcome("failed", f"run branch {run.branch} has no tip")

            ctx = await allocate_attempt(self, fresh, run, base_commit)
            attempt = ctx.attempt
            deadline = asyncio.get_running_loop().time() + self.settings.limits.attempt_wallclock_s
            step: VerificationResult | None = None
            cancelled = False
            try:
                runtime = build_runtime(self, run, fresh, attempt, ctx.container_id)
                try:
                    outcome = await runtime.execute_attempt(
                        spec_slice=fresh.spec_slice_md,
                        guidance=await codebase_index_guidance(
                            _effective_guidance(),
                            self.db,
                            fresh,
                            ctx.worktree.path,
                            self.settings.limits,
                        ),
                        deadline_s=deadline,
                    )
                    # Verify (+ merge unless deferred) while the container
                    # is still alive (the finally below tears it down).
                    step = (
                        await run_verification(
                            self,
                            run,
                            fresh,
                            ctx,
                            base_commit,
                            integrate=integrate,
                            agent_no_changes=outcome.no_changes,
                            turns_used=outcome.turns_used,
                        )
                        if outcome.status == "succeeded"
                        else None
                    )
                    salvage: tuple[str, list[str]] | None = None
                    if outcome.status in ("failed", "timeout"):
                        # Group B: preserve uncommitted work BEFORE the
                        # finally below force-prunes the worktree; a salvage
                        # failure must not mask the real outcome.
                        with suppress(Exception):
                            salvage = await salvage_worktree(self.repo_path, fresh, attempt)
                        if salvage is not None:
                            self._salvage_tips[fresh.id] = salvage[0]
                except BudgetExceeded:
                    # The gateway already froze attempt + run; report upward.
                    return TaskOutcome("budget_exhausted")
                except asyncio.CancelledError:
                    # Cancellation protocol: the run pump owns every terminal
                    # transition; kill this attempt's container best-effort
                    # (idempotent) and re-raise WITHOUT DB status writes.
                    cancelled = True
                    with suppress(Exception):
                        await self.sandbox.kill(ctx.container_id)
                    raise
                except Exception as exc:
                    # Never silently swallow programming errors — record, raise.
                    await record_engine_error(self, attempt, exc)
                    raise
            finally:
                await teardown_attempt(self, ctx, force=not cancelled)

            if outcome.status == "amendment_requested":
                reason, change = outcome.amendment or ("unspecified", "")
                await spec_amendment.request_spec_amendment(
                    self.db,
                    run=run,
                    task=fresh,
                    attempt=attempt,
                    reason=reason,
                    suggested_change=change,
                    notifier=self.notifier,
                )
                return TaskOutcome("amendment_pending")

            if outcome.status == "integrity_violation":
                # §8.2: the terminal turn was tainted by a held scope
                # violation; the ledger row was already written by the tool
                # registry (exactly once) — fail without retry here.
                reason = outcome.failure_reason or "held call in the terminal turn"
                await fail_without_retry(self, run, fresh, attempt, reason)
                return TaskOutcome("integrity_violation", reason)

            if outcome.status == "succeeded":
                if step is None:  # pragma: no cover - defensive
                    raise RuntimeError("succeeded outcome without a verify step")
                if step.kind == "retry":
                    guidance = f"Previous attempt failed verification: {step.detail}"
                    if step.salvage is not None:
                        guidance = salvage_guidance(
                            attempt.attempt_num,
                            "failed verification with uncommitted work",
                            step.salvage,
                            guidance,
                        )
                    continue
                return TaskOutcome(step.kind, step.detail, no_changes=step.no_changes)

            # timeout | failed (turn budget) — retry with feedback, or fail.
            tail = self._redact_tail(outcome.failure_reason or outcome.summary or "")
            await close_attempt(
                self, attempt.id, outcome.status, tail, turns_used=outcome.turns_used
            )
            next_guidance = await retry_or_fail(
                self,
                run,
                fresh,
                tail,
                event="attempt_retry_scheduled",
                note=f"attempt {outcome.status}",
            )
            if next_guidance is None:
                return TaskOutcome("failed", tail)
            guidance = f"Previous attempt {outcome.status}: {tail}"
            if salvage is not None:
                guidance = salvage_guidance(
                    attempt.attempt_num,
                    "ran out of turns after writing work",
                    salvage,
                    guidance,
                )
