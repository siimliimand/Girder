"""Attempt lifecycle + verification loop for one task (impl-plan §11 WP 3.5).

``execute_task`` drives, for one task, up to ``task_max_attempts`` sequential
attempts. Per attempt (§5.5 — park state first, act after):

1. task FSM parked (pending→scheduled→running / retry_scheduled→running),
2. attempt row + git worktree + container allocated,
3. the Layer-2 anchor (``tasks.test_content_hash``) captured from a
   :class:`~girder.gitops.audit.TestManifest` taken BEFORE the agent acts,
4. the agent turn loop runs inside the sandbox,
5. on success the suite runs in the same container, the container is then
   killed (D10: the orchestrator-side audit must observe a quiesced
   worktree — a hostile agent must not be able to mutate it between audit
   and merge), Layer 2/3 :class:`~girder.gitops.audit.DiffAudit` checks the
   observed diff against declared intent, and only a fully green audit
   reaches the ff-only audit-gated merge onto the run branch.

Integrity violations (plan §8.2) fail WITHOUT retry and never merge.
Gateway budget exceptions (:class:`~girder.budget.guard.BudgetExceeded`)
propagate out of the agent runtime — this engine catches them and reports a
``budget_exhausted`` outcome; the gateway has already frozen attempt + run.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from girder.agent.runtime import AgentRuntime
from girder.budget.guard import BudgetExceeded
from girder.config import Settings, project_allows_empty_baseline
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import (
    Attempt,
    AttemptStatus,
    IntegrityKind,
    Project,
    Run,
    SteeringKind,
    Task,
    TaskStatus,
    TaskType,
    Worktree,
    WorktreeState,
)
from girder.fsm import InvalidTransition, transition_attempt, transition_task
from girder.gitops.audit import AuditResult, DiffAudit, TestManifest
from girder.gitops.branch import BranchOps
from girder.gitops.worktree import DEFAULT_BASE, WorktreeManager, WorktreeRef
from girder.guard.redact import Redactor, redact_and_log
from girder.guard.scope import TaskScopes
from girder.models.gateway import ModelGateway
from girder.notify.notifier import Notifier
from girder.orchestrator.baseline import empty_suite_accepted, parse_junit_xml
from girder.sandbox.engine import ContainerSpec, SandboxEngine
from girder.specs import amendment as spec_amendment
from girder.stacks import get_stack  # WS-07A: per-stack verify suite (WP 11.1)
from girder.util import run_host_cmd, utcnow_iso

log = logging.getLogger(__name__)

VERIFY_XML = ".girder-verify.xml"


def verify_cmd(python_bin: str) -> list[str]:
    """Verification suite argv under the configured interpreter (§5.5)."""
    return [
        python_bin,
        "-m",
        "pytest",
        "-q",
        f"--junitxml=/workspace/{VERIFY_XML}",
        "-p",
        "no:cacheprovider",
    ]


# Default-interpreter form kept for callers that have no Settings in hand
# (orchestrator.suites); the engine itself uses ``verify_cmd(python_bin)``.
VERIFY_CMD = verify_cmd("python3")
_VERIFY_TIMEOUT_S = 900.0
_TAIL_CHARS = 2000


@dataclass(frozen=True)
class TaskOutcome:
    # completed | verified (wave mode, integration deferred) | failed |
    # integrity_violation | amendment_pending | budget_exhausted
    kind: str
    detail: str | None = None
    # True when the agent declared mark_task_complete(no_changes=true) and the
    # empty diff was accepted as a legitimate no-op (Group D).
    no_changes: bool = False


@dataclass(frozen=True)
class _VerifyStep:
    """Internal: completed/verified/failed/integrity_violation, or retry."""

    kind: str
    detail: str | None
    # no_changes=true declared by the agent (Group D — empty-diff honesty).
    no_changes: bool = False
    # (salvage sha, changed files) when the failed attempt's dirty worktree
    # was committed to the task branch before teardown (Group B).
    salvage: tuple[str, list[str]] | None = None


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
        # attempt; consumed once by _start_attempt so the retry worktree is
        # created at the salvaged task-branch tip instead of the run tip.
        self._salvage_tips: dict[str, str] = {}

    def _container_spec(self, name: str, worktree: Path) -> ContainerSpec:
        """Build the attempt ContainerSpec (impl-plan §6.4, R11).

        Forwards the operator's ``[sandbox]`` network/resource policy so the
        girder.toml settings actually reach the container; the config defaults
        equal the ContainerSpec dataclass defaults, so the default-config
        behavior is unchanged.
        """
        return ContainerSpec(
            name=name,
            image=self.image,
            worktree=worktree,
            network=self.settings.sandbox.network,
            memory=self.settings.sandbox.memory,
            cpus=self.settings.sandbox.cpus,
            pids_limit=self.settings.sandbox.pids_limit,
        )

    # ------------------------------------------------------------- entrypoint

    async def execute_task(self, run: Run, task: Task, *, integrate: bool = True) -> TaskOutcome:
        """Run *task* to a terminal outcome (or an amendment park).

        With ``integrate=False`` (Sprint 5 wave mode) a green, audit-clean
        attempt stops at ``verify_passed`` with its commits on the task branch
        — the wave integrator merges verified task branches one by one, so
        agents never merge into a shared branch concurrently (plan.md Phase 4
        task 4).
        """
        max_attempts = self.settings.limits.task_max_attempts
        # Rejection guidance (§6.9): a user-rejected spec amendment persists
        # guidance that must reach the agent as trusted steering. Looked up
        # once per execute_task call (i.e. once per amendment resume) and kept
        # as a standing constraint on every attempt, composed before any
        # failed-attempt feedback. User-authored ⇒ trusted per §7.
        rejection_guidance = await repo.get_rejected_guidance(self.db, run.id, task.id)
        # Sprint 6 (WP 6.2): steering injects that arrived between tasks or
        # during a pause were never consumed by an agent turn — fold them into
        # this attempt's guidance instead (blank-line separated).
        injects = await repo.consume_steering_events(
            self.db, run.id, kinds=[SteeringKind.INJECT.value]
        )
        directives = [
            str(e["payload"].get("directive", "") or "").strip()
            for e in injects
            if isinstance(e["payload"], dict)
            and str(e["payload"].get("directive", "") or "").strip()
        ]
        steering_guidance = "\n\n".join(
            g for g in (rejection_guidance, *directives) if g
        ) or None
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
                await self._fail_task(fresh, "attempt budget exhausted")
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
                await self._fail_task(fresh, f"run branch {run.branch} has no tip")
                return TaskOutcome("failed", f"run branch {run.branch} has no tip")

            attempt, worktree, manifest, container = await self._start_attempt(
                fresh, run, base_commit
            )
            deadline = asyncio.get_running_loop().time() + self.settings.limits.attempt_wallclock_s
            step: _VerifyStep | None = None
            cancelled = False
            try:
                runtime = AgentRuntime(
                    gateway=self.gateway,
                    sandbox=self.sandbox,
                    container=container,
                    scopes=TaskScopes(
                        write_globs=fresh.scope_globs,
                        protected_globs=self.settings.project.protected_read_paths,
                        strict_read_scope=self.settings.project.strict_read_scope,
                    ),
                    limits=self.settings.limits,
                    redactor=self.redactor,
                    db=self.db,
                    run_id=run.id,
                    attempt=attempt,
                    task=fresh,
                )
                try:
                    outcome = await runtime.execute_attempt(
                        spec_slice=fresh.spec_slice_md,
                        guidance=_effective_guidance(),
                        deadline_s=deadline,
                    )
                    # Verify (+ merge unless deferred) while the container is
                    # still alive (the finally below tears it down).
                    step = (
                        await self._verify_and_integrate(
                            run,
                            fresh,
                            attempt,
                            worktree,
                            manifest,
                            base_commit,
                            container,
                            integrate=integrate,
                            agent_no_changes=outcome.no_changes,
                            turns_used=outcome.turns_used,
                        )
                        if outcome.status == "succeeded"
                        else None
                    )
                    salvage: tuple[str, list[str]] | None = None
                    if outcome.status in ("failed", "timeout"):
                        # Group B: ordinary retryable death (turn cap /
                        # wall-clock) — preserve uncommitted work BEFORE the
                        # finally below force-prunes the worktree. Integrity
                        # violations and amendment parks never reach this.
                        # A salvage failure must not mask the real outcome.
                        with suppress(Exception):
                            salvage = await self._salvage_worktree(fresh, attempt)
                        if salvage is not None:
                            self._salvage_tips[fresh.id] = salvage[0]
                except BudgetExceeded:
                    # The gateway already froze the attempt (budget_frozen) and
                    # routed the run to budget_exhausted; report upward.
                    return TaskOutcome("budget_exhausted")
                except asyncio.CancelledError:
                    # Cancellation protocol: the run pump aborting the attempt
                    # owns every terminal transition (attempt → crashed, task
                    # state, events). Here we only kill this attempt's sandbox
                    # container — best-effort and idempotent, the pump may have
                    # killed it already — then re-raise WITHOUT any DB status
                    # writes (no attempt/task transitions, no events).
                    cancelled = True
                    with suppress(Exception):
                        await self.sandbox.kill(container)
                    raise
                except Exception as exc:
                    # Never silently swallow programming errors — record, raise.
                    with suppress(InvalidTransition):
                        await transition_attempt(self.db, attempt.id, AttemptStatus.FAILED)
                    await repo.update_attempt_fields(
                        self.db,
                        attempt.id,
                        failure_reason=f"engine error: {type(exc).__name__}: {exc}",
                        ended_at=utcnow_iso(),
                    )
                    raise
            finally:
                await self._teardown_attempt(attempt, container, record_end=not cancelled)

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
                # violation — the "completion" rests on work the registry
                # never executed. The ledger row was already written by the
                # tool registry (exactly once); fail without retry here.
                reason = outcome.failure_reason or "held call in the terminal turn"
                await self._fail_without_retry(run, fresh, attempt, reason)
                return TaskOutcome("integrity_violation", reason)

            if outcome.status == "succeeded":
                if step is None:  # pragma: no cover - defensive
                    raise RuntimeError("succeeded outcome without a verify step")
                if step.kind == "retry":
                    guidance = f"Previous attempt failed verification: {step.detail}"
                    if step.salvage is not None:
                        guidance = self._salvage_guidance(
                            attempt.attempt_num,
                            "failed verification with uncommitted work",
                            step.salvage,
                            guidance,
                        )
                    continue
                return TaskOutcome(step.kind, step.detail, no_changes=step.no_changes)

            # timeout | failed (turn budget) — retry with feedback, or fail.
            tail = self._redact_tail(outcome.failure_reason or outcome.summary or "")
            await self._close_attempt(
                attempt.id, outcome.status, tail, turns_used=outcome.turns_used
            )
            next_guidance = await self._retry_or_fail(
                run, fresh, tail, event="attempt_retry_scheduled", note=f"attempt {outcome.status}"
            )
            if next_guidance is None:
                return TaskOutcome("failed", tail)
            guidance = f"Previous attempt {outcome.status}: {tail}"
            if salvage is not None:
                guidance = self._salvage_guidance(
                    attempt.attempt_num,
                    "ran out of turns after writing work",
                    salvage,
                    guidance,
                )

    # ------------------------------------------------------- attempt plumbing

    async def _start_attempt(
        self, task: Task, run: Run, base_commit: str
    ) -> tuple[Attempt, WorktreeRef, TestManifest, str]:
        attempt = await repo.create_attempt(self.db, task.id, base_commit)
        manager = WorktreeManager(self.repo_path, self.worktree_base or DEFAULT_BASE)
        # Group B: a retry worktree must be created at the salvaged task-branch
        # tip — the default base (run-branch tip) does not contain the salvage
        # commit, and the retry would redo (or clobber) completed work. The
        # task branch persists across attempts (only the worktree is pruned),
        # and `worktree add -b` falls back to a detached checkout at this base
        # when the branch already exists.
        wt_base_commit = self._salvage_tips.pop(task.id, base_commit)
        try:
            ref = await manager.create(run.id, task.id, wt_base_commit)
        except FileExistsError:
            # A prior attempt crashed mid-_start_attempt (after provisioning,
            # before its teardown could prune). Attempts are sequential per
            # task, so the path can only be an orphan here — clear it and
            # provision fresh.
            await manager.heal_orphan(run.id, task.id)
            ref = await manager.create(run.id, task.id, wt_base_commit)
        await repo.create_worktree(self.db, attempt.id, str(ref.path), ref.branch)
        attempt.worktree_path = str(ref.path)  # keep the local object in sync for teardown
        await repo.update_attempt_fields(
            self.db, attempt.id, worktree_path=str(ref.path), started_at=utcnow_iso()
        )
        manifest = await self._audit.capture_test_manifest(ref.path)
        # Layer 2 anchor: hash of every test-signal file before the agent acts.
        await repo.update_task_fields(self.db, task.id, test_content_hash=manifest.root_hash)

        spec = self._container_spec(f"girder-{attempt.id[:8]}", ref.path)
        # Layer 1 test shadowing (§8.2): mount a pristine RO snapshot of each
        # configured test directory over the worktree's own copy — one
        # snapshot dir + one RO shadow mount per test directory (the engine
        # renders ``ro_mounts`` as ``-v host:container:ro`` before the
        # workspace contents are visible to the agent). With no configured
        # test directories Layer 1 is inactive — warn, and rely on Layers 2
        # (re-hash) and 3 (diff audit) to catch tampering after the fact.
        test_dirs = self.settings.project.test_directories
        snapshot_dirs: list[Path] = []
        if task.task_type is not TaskType.TEST_CHANGE:
            if not test_dirs:
                log.warning(
                    "attempt %s: project.test_directories is empty — "
                    "Layer 1 RO test snapshot is INACTIVE (§8.2)",
                    attempt.id,
                )
            for test_dir in test_dirs:
                # A test-less repo (allow_empty_baseline) has no test dirs at
                # the base commit: skip the snapshot instead of crashing the
                # attempt — Layer 1 has nothing to protect there; Layers 2/3
                # still cover the (empty) test-signal surface.
                if not await self._audit.commit_has_path(self.repo_path, base_commit, test_dir):
                    log.info(
                        "attempt %s: test dir %r absent at base commit — "
                        "Layer 1 RO snapshot skipped",
                        attempt.id,
                        test_dir,
                    )
                    continue
                snapshot_dir = Path(tempfile.mkdtemp(prefix="girder-snap-"))
                await self._audit.materialize_test_snapshot(
                    self.repo_path, base_commit, [test_dir], snapshot_dir
                )
                # `git archive` keeps the path prefix, so the shadow source is
                # <snapshot>/<test_dir>, mounted over /workspace/<test_dir>.
                spec.ro_mounts[str(snapshot_dir / test_dir.strip("/"))] = (
                    "/workspace/" + test_dir.strip("/")
                )
                snapshot_dirs.append(snapshot_dir)
        # §8.1 package cache (plan.md Phase 0 task 3, impl-plan §6.4): bind the
        # host cache dirs read-only at the paths the image's env vars point at
        # (PIP_CACHE_DIR/npm_config_cache/CARGO_HOME = /cache/*) so pip/npm/
        # cargo never fetch over the public internet per worktree. A missing
        # host subdir (dev machine, first boot) just skips that mount.
        cache_root = Path(self.settings.sandbox.cache_dir)
        for cache_sub, container_path in (
            ("pip", "/cache/pip"),
            ("npm", "/cache/npm"),
            ("cargo", "/cache/cargo"),
        ):
            host_dir = cache_root / cache_sub
            if host_dir.is_dir():
                spec.ro_mounts[str(host_dir)] = container_path
            else:
                log.debug(
                    "attempt %s: package cache %s missing — RO cache mount skipped",
                    attempt.id,
                    host_dir,
                )
        self._snapshot_dirs[spec.name] = snapshot_dirs

        await self.sandbox.start(spec)
        await repo.update_attempt_fields(self.db, attempt.id, container_id=spec.name)
        await transition_attempt(self.db, attempt.id, AttemptStatus.RUNNING)
        return attempt, ref, manifest, spec.name

    async def _teardown_attempt(
        self, attempt: Attempt, container: str, *, record_end: bool = True
    ) -> None:
        """Tear down one attempt's container, snapshots and worktree.

        With ``record_end=False`` (cancellation protocol: the run pump owns
        the terminal transition) only the container kill and snapshot cleanup
        run — no worktree pruning, no DB writes.
        """
        with suppress(Exception):
            await self.sandbox.kill(container)
        for snapshot_dir in self._snapshot_dirs.pop(container, []):
            shutil.rmtree(snapshot_dir, ignore_errors=True)
        if not record_end:
            return
        wt_path = attempt.worktree_path
        if wt_path is not None:
            manager = WorktreeManager(self.repo_path, self.worktree_base or DEFAULT_BASE)
            try:
                await manager.remove(wt_path, force=True)
                row = await self._worktree_row(attempt.id)
                if row is not None:
                    await repo.set_worktree_state(self.db, row.id, WorktreeState.PRUNED)
            except Exception:  # pragma: no cover - git failures leave it to the GC
                log.exception("failed to prune worktree %s", wt_path)
        await repo.update_attempt_fields(self.db, attempt.id, ended_at=utcnow_iso())

    async def _kill_attempt_container(self, container: str) -> None:
        """Kill one attempt's container and its RO test snapshots, best-effort.

        Idempotent: the engine-level ``finally`` teardown repeats both steps
        (a second kill is a no-op, the snapshot map entry is already gone).
        """
        with suppress(Exception):
            await self.sandbox.kill(container)
        for snapshot_dir in self._snapshot_dirs.pop(container, []):
            shutil.rmtree(snapshot_dir, ignore_errors=True)

    async def _worktree_row(self, attempt_id: str) -> Worktree | None:
        for row in await repo.list_worktrees(self.db):
            if row.attempt_id == attempt_id:
                return row
        return None

    # ----------------------------------------------------- verify & integrate

    async def _verify_and_integrate(
        self,
        run: Run,
        task: Task,
        attempt: Attempt,
        worktree: WorktreeRef,
        manifest: TestManifest,
        base_commit: str,
        container: str,
        *,
        integrate: bool = True,
        agent_no_changes: bool = False,
        turns_used: int | None = None,
    ) -> _VerifyStep:
        await transition_task(self.db, task.id, TaskStatus.VERIFYING)
        # §8 (impl-plan): integrity violations block the merge at every tier —
        # ANY held/scope-violating tool call taints this attempt, even when the
        # terminal turn itself was clean (runtime.py only catches the
        # same-turn case). The ledger rows were already written by the tool
        # registry (exactly once); fail the attempt without retry here.
        held_count = await repo.count_held_tool_calls(self.db, attempt.id)
        if held_count:
            reason = f"{held_count} held scope-violating tool call(s) on attempt {attempt.id}"
            await self._fail_without_retry(run, task, attempt, reason)
            return _VerifyStep("integrity_violation", reason)
        exec_res = await self.sandbox.exec(
            container,
            # WS-07A (WP 11.1): the stack plugin resolves the verify suite;
            # PythonPlugin returns exactly verify_cmd(python_bin) (SC-01 guard).
            get_stack(self.settings.project.stack).test_command(
                self.settings.sandbox.python_bin
            ),
            timeout_s=_VERIFY_TIMEOUT_S,
        )
        xml_path = worktree.path / VERIFY_XML
        allow_empty = project_allows_empty_baseline(self.repo_path)
        suite_green = (
            exec_res.exit_code == 0 or empty_suite_accepted(exec_res.exit_code, allow_empty)
        ) and xml_path.is_file()
        test_results: dict[str, str] = {}
        if xml_path.is_file():
            try:
                test_results = {
                    tid: r.status for tid, r in parse_junit_xml(xml_path.read_text()).items()
                }
                suite_green = suite_green and (bool(test_results) or allow_empty)
            except Exception:
                suite_green = False
            # The report is orchestrator plumbing, not task output: remove it
            # so the uncommitted-leftover audit check doesn't flag it.
            xml_path.unlink(missing_ok=True)

        if not suite_green:
            tail = self._redact_tail(exec_res.stdout + "\n" + exec_res.stderr)
            return await self._retry_step(
                run,
                task,
                attempt,
                tail,
                event="verify_failed",
                note="verification suite failed",
                turns_used=turns_used,
            )

        # Suite green — kill the container BEFORE any orchestrator-side audit
        # (plan.md D10 / Phase 0 task 5): a hostile agent could plant a
        # delayed background process that mutates the worktree between the
        # audit and the merge; the audit must observe a quiesced tree. The
        # suite itself ran in-container above. The engine-level `finally` in
        # execute_task stays as the backstop kill on every other path.
        await self._kill_attempt_container(container)

        # Layer 2/3 mechanical audit of the observed diff.
        audit = await self._audit.audit_attempt(
            worktree.path,
            task_type=task.task_type,
            scope_globs=task.scope_globs,
            base_commit=base_commit,
            start_manifest=manifest,
        )
        if audit.uncommitted_leftovers and not (
            audit.test_path_violations
            or audit.content_hash_mismatches
            or audit.out_of_scope_writes
            or audit.protected_path_touches
        ):
            # §5.2: forgetting to commit is an ordinary attempt failure —
            # NOT an integrity violation (§8.2 lists only the five mechanical
            # kinds below). Retry with explicit commit guidance.
            detail = (
                "uncommitted changes left on the task branch ("
                + ", ".join(audit.uncommitted_leftovers[:5])
                + "). Commit your work (git add -A && git commit); "
                "the task branch must be clean before completion."
            )
            return await self._retry_step(
                run,
                task,
                attempt,
                detail,
                event="verify_failed",
                note="uncommitted leftovers",
                turns_used=turns_used,
            )
        if not audit.passed:
            return await self._integrity_violation(run, task, attempt, audit)

        # A clean tree with an empty commit range is a legitimate "no change
        # needed" outcome (e.g. documentation-only intents) — but only when
        # the agent declared it via mark_task_complete(no_changes=true)
        # (Group D no-op honesty): otherwise a silent no-op is an ordinary
        # retryable failure. "Forgot to commit" is caught above via the
        # uncommitted-leftovers audit finding, not here.

        # Record where this attempt's work ended on the task branch (the
        # worktree may be detached after a retry), then merge ff-only.
        head = await run_host_cmd(
            ["git", "-C", str(worktree.path), "rev-parse", "HEAD"], timeout_s=30
        )
        if head.stdout.strip() == base_commit and not agent_no_changes:
            detail = (
                "you declared the task complete but no changes exist; if genuinely "
                "nothing is needed, call mark_task_complete(no_changes=true); "
                "otherwise write the change."
            )
            return await self._retry_step(
                run,
                task,
                attempt,
                detail,
                event="verify_failed",
                note="empty diff without no_changes",
                turns_used=turns_used,
            )
        await run_host_cmd(
            [
                "git",
                "-C",
                str(self.repo_path),
                "update-ref",
                f"refs/heads/{worktree.branch}",
                head.stdout.strip(),
            ],
            timeout_s=30,
        )
        # Persist the attempt diff for the console diff viewer (migration 012,
        # impl-plan §10): caller redacts before insert (repo contract).
        diff_res = await run_host_cmd(
            ["git", "-C", str(worktree.path), "diff", f"{base_commit}..{head.stdout.strip()}"],
            timeout_s=30,
        )
        diff_redacted = await redact_and_log(
            self.redactor,
            diff_res.stdout,
            source_field="attempt_diff",
            db=self.db,
            attempt_id=attempt.id,
        )
        await repo.insert_attempt_diff(
            self.db,
            attempt_id=attempt.id,
            task_id=task.id,
            run_id=run.id,
            base_commit=base_commit,
            head_commit=head.stdout.strip(),
            diff_redacted=diff_redacted,
        )
        if not integrate:
            # Wave mode (Phase 4): stop at verify_passed. The branch tip is
            # recorded; the wave integrator merges verified branches one by
            # one, so concurrent agents never share a merge target.
            await transition_attempt(self.db, attempt.id, AttemptStatus.SUCCEEDED)
            if turns_used is not None:
                await repo.update_attempt_fields(self.db, attempt.id, turns_used=turns_used)
            await repo.insert_event(
                self.db,
                "task_verified",
                {
                    "task_id": task.id,
                    "attempt_id": attempt.id,
                    "commit": head.stdout.strip(),
                    "tests": len(test_results),
                },
                run_id=run.id,
                attempt_id=attempt.id,
            )
            await transition_task(self.db, task.id, TaskStatus.VERIFY_PASSED)
            return _VerifyStep("verified", head.stdout.strip(), no_changes=agent_no_changes)
        merge = await BranchOps(self.repo_path).audit_gated_merge(
            source_branch=worktree.branch,
            target_branch=run.branch,
            audit_passed_for_commit=head.stdout.strip(),
            worktree_path=worktree.path,
        )
        if not merge.merged:
            # Unexpected refusal after a passed audit (§5.2): an ordinary
            # attempt failure — retry, never logged as an integrity violation
            # (§8.2 reserves that for the five mechanical kinds only).
            reason = merge.reason or "merge refused"
            detail = f"merge refused after a passed audit: {reason}"
            return await self._retry_step(
                run,
                task,
                attempt,
                detail,
                event="merge_refused",
                note=detail,
                turns_used=turns_used,
            )

        await transition_attempt(self.db, attempt.id, AttemptStatus.SUCCEEDED)
        if turns_used is not None:
            await repo.update_attempt_fields(self.db, attempt.id, turns_used=turns_used)
        await repo.insert_event(
            self.db,
            "task_completed",
            {
                "task_id": task.id,
                "attempt_id": attempt.id,
                "merged_commit": merge.merged_commit,
                "tests": len(test_results),
            },
            run_id=run.id,
            attempt_id=attempt.id,
        )
        await transition_task(self.db, task.id, TaskStatus.VERIFY_PASSED)
        await transition_task(self.db, task.id, TaskStatus.COMPLETED)
        return _VerifyStep("completed", merge.merged_commit, no_changes=agent_no_changes)

    async def _integrity_violation(
        self, run: Run, task: Task, attempt: Attempt, audit: AuditResult
    ) -> _VerifyStep:
        # §8.2: integrity violations fail WITHOUT retry and never merge. Only
        # the five mechanical kinds count — uncommitted leftovers are handled
        # upstream as an ordinary retryable failure.
        findings: list[tuple[IntegrityKind, dict[str, object]]] = []
        if audit.test_path_violations:
            findings.append(
                (IntegrityKind.TEST_PATH_MODIFIED, {"paths": audit.test_path_violations})
            )
        if audit.content_hash_mismatches:
            findings.append(
                (IntegrityKind.CONTENT_HASH_MISMATCH, {"paths": audit.content_hash_mismatches})
            )
        if audit.out_of_scope_writes:
            findings.append(
                (IntegrityKind.OUT_OF_SCOPE_WRITE, {"paths": audit.out_of_scope_writes})
            )
        if audit.protected_path_touches:
            findings.append(
                (IntegrityKind.PROTECTED_READ, {"paths": audit.protected_path_touches})
            )
        if not findings:  # pragma: no cover - guarded by the callers above
            raise RuntimeError("integrity violation with no mechanical findings")
        for kind, detail in findings:
            await repo.insert_integrity_violation(
                self.db, run.id, kind.value, detail, task_id=task.id, attempt_id=attempt.id
            )
        summary = "; ".join(kind.value for kind, _ in findings)
        await self._fail_without_retry(run, task, attempt, summary)
        return _VerifyStep("integrity_violation", summary)

    async def _fail_without_retry(
        self, run: Run, task: Task, attempt: Attempt, reason: str
    ) -> None:
        await transition_attempt(self.db, attempt.id, AttemptStatus.INTEGRITY_VIOLATION)
        await repo.update_attempt_fields(
            self.db, attempt.id, failure_reason=reason, ended_at=utcnow_iso()
        )
        await self._fail_task(task, reason)
        if self.notifier is not None:
            await self.notifier.notify(
                "error",
                "Girder: integrity violation — task failed without retry",
                f"run {run.id} task {task.id}: {reason}",
                run_id=run.id,
            )

    # ---------------------------------------------------------------- helpers

    async def _retry_step(
        self,
        run: Run,
        task: Task,
        attempt: Attempt,
        detail: str,
        *,
        event: str,
        note: str,
        turns_used: int | None = None,
    ) -> _VerifyStep:
        """Close the attempt FAILED and retry with feedback, or fail the task."""
        # Group B: salvage any uncommitted work before the engine-level
        # finally prunes the worktree — verify-fail retries are ordinary
        # retries, so the same salvage semantics apply.
        salvage: tuple[str, list[str]] | None = None
        with suppress(Exception):
            salvage = await self._salvage_worktree(task, attempt)
        if salvage is not None:
            self._salvage_tips[task.id] = salvage[0]
        tail = self._redact_tail(detail)
        await self._close_attempt(
            attempt.id, AttemptStatus.FAILED.value, tail, turns_used=turns_used
        )
        next_guidance = await self._retry_or_fail(run, task, tail, event=event, note=note)
        if next_guidance is None:
            return _VerifyStep("failed", tail)
        # Feedback to the next attempt must be the redacted tail (Phase 2
        # task 5): the raw detail can carry git stderr with secret-shaped
        # content (e.g. a merge-refusal echoing a token).
        return _VerifyStep("retry", tail, salvage=salvage)

    async def _salvage_worktree(self, task: Task, attempt: Attempt) -> tuple[str, list[str]] | None:
        """Commit a dead attempt's dirty worktree onto its task branch (Group B).

        Live dogfood runs lost every retry: the dead attempt's uncommitted
        worktree was force-pruned, so attempt N+1 re-explored from scratch and
        died identically. Committing the leftovers (mechanically, with a
        pinned identity so a missing host git config can't fail) keeps that
        work; `_start_attempt` then bases the retry worktree on the salvaged
        tip. Integrity violations and amendment parks keep force-prune
        semantics — their callers never invoke this.
        """
        wt = attempt.worktree_path
        if wt is None:
            return None
        status = await run_host_cmd(
            ["git", "-C", wt, "status", "--porcelain"], check=False, timeout_s=30
        )
        if status.returncode != 0 or not status.stdout.strip():
            return None
        await run_host_cmd(["git", "-C", wt, "add", "-A"], check=False, timeout_s=60)
        commit = await run_host_cmd(
            [
                "git",
                "-C",
                wt,
                "-c",
                "user.name=girder",
                "-c",
                "user.email=girder@local",
                "commit",
                "-m",
                f"wip(attempt {attempt.attempt_num}): salvaged on retry",
            ],
            check=False,
            timeout_s=60,
        )
        if commit.returncode != 0:
            log.warning(
                "attempt %s: salvage commit failed: %s", attempt.id, commit.stderr[-200:]
            )
            return None
        head = await run_host_cmd(["git", "-C", wt, "rev-parse", "HEAD"], timeout_s=30)
        sha = head.stdout.strip()
        files = await run_host_cmd(
            ["git", "-C", wt, "diff", "--name-only", "HEAD~1"], check=False, timeout_s=30
        )
        # Advance the task branch even when the worktree HEAD is detached
        # (retry worktrees are created detached once the branch exists), so
        # the salvage is reachable for the retry base and post-mortems.
        await run_host_cmd(
            [
                "git",
                "-C",
                str(self.repo_path),
                "update-ref",
                f"refs/heads/task/{task.id}",
                sha,
            ],
            timeout_s=30,
        )
        changed = [ln for ln in files.stdout.splitlines() if ln.strip()]
        log.info(
            "attempt %s: salvaged uncommitted work as %s (%d file(s))",
            attempt.id,
            sha[:12],
            len(changed),
        )
        return sha, changed

    def _salvage_guidance(
        self, attempt_num: int, fate: str, salvage: tuple[str, list[str]], base: str
    ) -> str:
        """Trusted retry guidance pointing attempt N+1 at the salvaged commit."""
        sha, files = salvage
        return (
            f"{base}\nPrevious attempt {attempt_num} {fate}; its uncommitted work was "
            f"salvaged as commit {sha} (files: {', '.join(files)}). Continue from "
            "there — do not redo completed work; finish and call mark_task_complete."
        )

    async def _retry_or_fail(
        self, run: Run, task: Task, tail: str, *, event: str, note: str
    ) -> str | None:
        """Retry-schedule with feedback, or fail the task; returns next guidance."""
        fresh = await repo.get_task(self.db, task.id)
        if fresh is None:
            return None
        await repo.insert_event(
            self.db, event, {"task_id": task.id, "note": note, "tail": tail}, run_id=run.id
        )
        if fresh.attempts_used < self.settings.limits.task_max_attempts:
            await transition_task(
                self.db, fresh.id, TaskStatus.RETRY_SCHEDULED, payload={"reason": note}
            )
            return tail
        await self._fail_task(fresh, note)
        return None

    async def _fail_task(self, task: Task, reason: str) -> None:
        fresh = await repo.get_task(self.db, task.id)
        if fresh is None:
            return
        with suppress(InvalidTransition):
            await transition_task(self.db, fresh.id, TaskStatus.FAILED, payload={"reason": reason})
        if self.notifier is not None:
            await self.notifier.notify(
                "error",
                "Girder: task failed",
                f"task {task.id} ({task.title}): {reason}",
            )

    async def _close_attempt(
        self, attempt_id: str, status: str, reason: str, *, turns_used: int | None = None
    ) -> None:
        with suppress(InvalidTransition):
            await transition_attempt(self.db, attempt_id, status)
        fields: dict[str, str | int | None] = {"failure_reason": reason, "ended_at": utcnow_iso()}
        if turns_used is not None:
            # Group G: every close path persists the agent turn count.
            fields["turns_used"] = turns_used
        await repo.update_attempt_fields(self.db, attempt_id, **fields)

    def _redact_tail(self, text: str) -> str:
        redacted, _ = self.redactor.redact(text[-_TAIL_CHARS:])
        return redacted
