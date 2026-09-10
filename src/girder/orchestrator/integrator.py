"""Wave integration — serialized, audit-gated merges into a wave branch.

Phase 4 (plan.md tasks 4-5, impl-plan §6.12 WP 5.2/5.3): concurrent wave
tasks never merge into a shared branch themselves. When a wave's tasks have
all reached ``verify_passed`` (the deferred-integration mode of
:class:`~girder.orchestrator.task_engine.TaskEngine`), this engine integrates
them **one by one**, in ``seq`` order, into ``wave/<run-id8>/w<seq>``:

1. an integration-time Layer 2/3 audit of the exact task-branch commit
   (:meth:`DiffAudit.audit_commit` — the attempt worktree is already gone),
2. the merge itself: fast-forward when possible, else a clean three-way
   merge commit (:meth:`BranchOps.integrate_branch`); a textual conflict is
   handed to the :class:`~girder.orchestrator.conflict.ConflictResolver`,
3. the **full local suite after every single merge** — a red suite localizes
   the semantic breakage to the task whose merge just landed, which then goes
   through the hardened resolution path (mandatory full re-test + targeted
   hunk conformance, cap ``limits.conflict_resolution_attempts``); if the
   resolution is exhausted, the task is dropped and the run escalated
   regardless of autonomy tier.

After the last wave task, the run branch is fast-forwarded to the wave tip
(it is an ancestor by construction), so the Sprint 4 delivery pipeline picks
up from there unchanged.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from girder.config import Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import (
    IntegrityKind,
    Project,
    Run,
    Task,
    TaskStatus,
    TaskType,
    Wave,
)
from girder.fsm import InvalidTransition, transition_task
from girder.github.conformance import HunkConformanceReviewer
from girder.gitops.audit import AuditResult, DiffAudit, TestManifest
from girder.gitops.branch import BranchOps
from girder.gitops.worktree import DEFAULT_BASE, WorktreeManager
from girder.guard.redact import Redactor
from girder.models.gateway import ModelGateway
from girder.notify.notifier import Notifier
from girder.orchestrator.conflict import ConflictResolver, ResolutionOutcome
from girder.orchestrator.suites import SuiteResult, run_suite_in_container
from girder.sandbox.engine import ContainerSpec, SandboxEngine
from girder.util import run_host_cmd

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class IntegrationOutcome:
    # wave_completed | escalated | budget_exhausted | failed
    kind: str
    detail: str | None = None


def wave_branch_name(run_id: str, sequence_order: int) -> str:
    """Wave integration branch: ``wave/<run-id8>/w<seq>`` (impl-plan §6.12)."""
    return f"wave/{run_id[:8]}/w{sequence_order}"


class WaveIntegrator:
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
        self.worktree_base = worktree_base or DEFAULT_BASE
        self.image = f"girder-runner:{settings.project.stack}"
        self._audit = DiffAudit(
            test_signal_patterns=settings.project.test_signal_patterns,
            protected_read_paths=settings.project.protected_read_paths,
        )

    # ------------------------------------------------------------------ entry

    async def integrate_wave(self, run: Run, wave: Wave) -> IntegrationOutcome:
        branch_ops = BranchOps(self.repo_path)
        run_tip = await branch_ops.run_branch_tip(run.branch)
        if run_tip is None:
            return IntegrationOutcome("failed", f"run branch {run.branch} has no tip")
        wave_branch = wave_branch_name(run.id, wave.sequence_order)
        await branch_ops.ensure_run_branch(wave_branch, base_commit=run_tip)

        resolver = ConflictResolver(
            db=self.db,
            gateway=self.gateway,
            sandbox=self.sandbox,
            settings=self.settings,
            redactor=self.redactor,
            notifier=self.notifier,
            project=self.project,
            repo_path=self.repo_path,
            hunk_reviewer=HunkConformanceReviewer(
                db=self.db, gateway=self.gateway, redactor=self.redactor
            ),
            worktree_base=self.worktree_base,
        )

        wave_tip = await branch_ops.run_branch_tip(wave_branch)
        assert wave_tip is not None  # ensured above
        tasks = await repo.list_tasks_for_wave(self.db, wave.id)
        # Resolution attempts may touch either side of a conflicted
        # interaction: their write scope is the union of the wave's scopes
        # (test files stay protected by the audit regardless).
        wave_scope = sorted({glob for t in tasks for glob in t.scope_globs})
        for task in tasks:
            fresh = await repo.get_task(self.db, task.id)
            if fresh is None:  # pragma: no cover - defensive
                return IntegrationOutcome("failed", f"task {task.id} vanished")
            if fresh.status is TaskStatus.COMPLETED:
                continue
            if fresh.status is not TaskStatus.VERIFY_PASSED:
                # Failed/dropped/skipped tasks never enter integration; a
                # failed task fails the run at the run level — surface it.
                if fresh.status is TaskStatus.FAILED:
                    return IntegrationOutcome("failed", f"task {task.id} is failed")
                continue

            outcome = await self._integrate_task(
                run, wave, wave_branch, fresh, resolver, wave_tip, wave_scope
            )
            if outcome.kind != "task_integrated":
                return IntegrationOutcome(outcome.kind, outcome.detail)
            wave_tip = outcome.detail or wave_tip

        # Wave done: audit the TIP itself before the ff. Each per-task merge
        # was audited individually, but the merged combination — the wave
        # tip — never was (plan.md Phase 4 task 5 / D4: nothing crosses a
        # branch boundary unaudited). Base is the run branch's pre-wave tip,
        # i.e. the commit the wave branch was created from. If any wave task
        # is a test change, its test-file writes were already legitimate
        # under that task's own audit, so the tip audit adopts TEST_CHANGE
        # semantics; with only code tasks, test files stay immutable.
        wave_has_test_change = any(t.task_type is TaskType.TEST_CHANGE for t in tasks)
        tip_audit = await self._audit.audit_commit(
            self.repo_path,
            task_type=TaskType.TEST_CHANGE if wave_has_test_change else TaskType.CODE_CHANGE,
            scope_globs=wave_scope,
            base_commit=run_tip,
            commit=wave_tip,
        )
        if not tip_audit.passed:
            return await self._tip_audit_escalation(run, tip_audit)

        # The run branch is an ancestor of the wave tip (the wave branch was
        # created from it), so the Sprint 4 delivery pipeline picks up from
        # there via the standard ff-only gate.
        ff = await branch_ops.audit_gated_merge(
            source_branch=wave_branch,
            target_branch=run.branch,
            audit_passed_for_commit=wave_tip,
        )
        if not ff.merged:
            return IntegrationOutcome("failed", f"run branch ff failed: {ff.reason}")
        await repo.insert_event(
            self.db,
            "wave_integrated",
            {"wave_id": wave.id, "sequence_order": wave.sequence_order, "tip": wave_tip},
            run_id=run.id,
        )
        return IntegrationOutcome("wave_completed", wave_tip)

    # --------------------------------------------------------------- per task

    async def _integrate_task(
        self,
        run: Run,
        wave: Wave,
        wave_branch: str,
        task: Task,
        resolver: ConflictResolver,
        wave_tip: str,
        wave_scope: list[str],
    ) -> IntegrationOutcome:
        branch_ops = BranchOps(self.repo_path)
        task_branch = f"task/{task.id}"
        task_tip = await branch_ops.run_branch_tip(task_branch)
        if task_tip is None:
            return IntegrationOutcome("escalated", f"task branch {task_branch} missing")

        # Already integrated (crash-resume): idempotence via ancestry.
        if await self._is_ancestor(task_tip, wave_tip):
            await self._mark_integrated(run, task, wave_tip)
            return IntegrationOutcome("task_integrated", wave_tip)

        # Integration-time Layer 2/3 audit of the exact commit being merged.
        audit = await self._audit_integrate(task, task_tip)
        if not audit.passed:
            return await self._audit_escalation(run, task, audit)

        pre_tip = wave_tip
        result = await branch_ops.integrate_branch(
            source_branch=task_branch,
            target_branch=wave_branch,
            audit_passed=True,
            message=f"Merge task {task.id} ({task.title}) into {wave_branch}",
        )

        new_tip: str
        # Set when a conflict resolution already ran the FULL suite green at
        # exactly this commit (impl-plan §6.12 WP 5.3 exception): the
        # per-merge re-run below is then redundant and skipped — fail-closed,
        # i.e. only an exact sha match skips, anything absent re-runs.
        suite_verified_at: str | None = None
        if result.merged:
            assert result.merged_commit is not None
            new_tip = result.merged_commit
        elif result.reason == "merge conflict":
            resolution = await resolver.resolve(
                run=run,
                task=task,
                wave_branch=wave_branch,
                kind="git_conflict",
                work_base=pre_tip,
                task_branch=task_branch,
                failure_tail="conflicted files: " + ", ".join(result.conflicted_files),
                wave_scope_globs=wave_scope,
            )
            after = await self._after_resolution(run, task, wave_branch, pre_tip, resolution)
            if isinstance(after, IntegrationOutcome):
                return after
            new_tip = after
            if resolution.kind == "resolved" and resolution.commit is not None:
                suite_verified_at = resolution.commit
        else:
            return IntegrationOutcome("failed", f"integration refused: {result.reason}")

        # Semantic gate (WP 5.3): the full suite runs after EVERY merge, so
        # breakage localizes to the task whose merge just landed — unless the
        # landed commit IS the conflict-resolution commit whose mandatory
        # full re-test already passed.
        if suite_verified_at is not None and new_tip == suite_verified_at:
            log.info(
                "run %s wave %s: suite already green at resolution commit %s — skipping re-run",
                run.id, wave.sequence_order, new_tip,
            )
        else:
            suite = await self._suite_at_commit(wave, task, new_tip)
            if not suite.green:
                resolution = await resolver.resolve(
                    run=run,
                    task=task,
                    wave_branch=wave_branch,
                    kind="semantic",
                    work_base=new_tip,
                    task_branch=task_branch,
                    failure_tail=suite.tail,
                    wave_scope_globs=wave_scope,
                )
                fixed = await self._after_resolution(run, task, wave_branch, new_tip, resolution)
                if isinstance(fixed, IntegrationOutcome):
                    return fixed
                new_tip = fixed

        await self._mark_integrated(run, task, new_tip)
        return IntegrationOutcome("task_integrated", new_tip)

    async def _after_resolution(
        self,
        run: Run,
        task: Task,
        wave_branch: str,
        pre_tip: str,
        resolution: ResolutionOutcome,
    ) -> IntegrationOutcome | str:
        """Map a resolution outcome onto the wave branch.

        Returns the new tip (str) on success, or an IntegrationOutcome to
        bubble up (escalation rolls the branch back to *pre_tip* first).
        """
        if resolution.kind == "budget_exhausted":
            return IntegrationOutcome("budget_exhausted", resolution.detail)
        if resolution.kind != "resolved":
            if pre_tip != await self._branch_tip(wave_branch):
                # Semantic case: the broken merge is on the branch — roll it
                # back out before escalating.
                await self._move_branch(wave_branch, pre_tip)
            return await self._drop_and_escalate(
                run, task, f"resolution failed: {resolution.detail}"
            )
        commit = resolution.commit or pre_tip
        if commit != pre_tip:
            # The resolution was committed in a detached worktree: land it on
            # the wave branch here (CAS from the pre-resolution tip).
            await self._move_branch(wave_branch, commit, expect=pre_tip)
        return commit

    # ---------------------------------------------------------------- helpers

    async def _audit_integrate(self, task: Task, task_tip: str) -> AuditResult:
        start_manifest: TestManifest | None = None
        if task.test_content_hash:
            start_manifest = TestManifest(root_hash=task.test_content_hash, files={})
        base_commit = await self._task_base_commit(task.id)
        return await self._audit.audit_commit(
            self.repo_path,
            task_type=task.task_type,
            scope_globs=task.scope_globs,
            base_commit=base_commit,
            commit=task_tip,
            start_manifest=start_manifest,
        )

    async def _task_base_commit(self, task_id: str) -> str:
        attempts = await repo.list_attempts_for_task(self.db, task_id)
        if attempts:
            return attempts[-1].base_commit
        # Defense in depth: fall back to the task branch tip itself.
        tip = await self._branch_tip(f"task/{task_id}")
        return tip or "HEAD"

    async def _suite_at_commit(self, wave: Wave, task: Task, commit: str) -> SuiteResult:
        """Full suite at *commit* in a throwaway detached worktree+container."""
        manager = WorktreeManager(self.repo_path, self.worktree_base)
        path = await manager.create_detached(
            run_id=wave.run_id,
            label=f"int-w{wave.sequence_order}-{task.id[:8]}",
            commit=commit,
        )
        # Forward the operator's [sandbox] policy (cf. task_engine._container_spec).
        spec = ContainerSpec(
            name=f"girder-int-{wave.id[:8]}-{task.id[:8]}",
            image=self.image,
            worktree=path,
            network=self.settings.sandbox.network,
            memory=self.settings.sandbox.memory,
            cpus=self.settings.sandbox.cpus,
            pids_limit=self.settings.sandbox.pids_limit,
        )
        await self.sandbox.start(spec)
        try:
            return await run_suite_in_container(self.sandbox, spec.name, path, self.redactor)
        finally:
            with suppress(Exception):
                await self.sandbox.kill(spec.name)
            with suppress(Exception):
                await manager.remove(path, force=True)

    async def _mark_integrated(self, run: Run, task: Task, commit: str) -> None:
        fresh = await repo.get_task(self.db, task.id)
        if fresh is not None and fresh.status is TaskStatus.VERIFY_PASSED:
            await transition_task(self.db, task.id, TaskStatus.COMPLETED)
        await repo.insert_event(
            self.db,
            "task_integrated",
            {"task_id": task.id, "commit": commit},
            run_id=run.id,
        )

    async def _drop_and_escalate(
        self, run: Run, task: Task, reason: str
    ) -> IntegrationOutcome:
        """Phase 4 task 5: drop the task from the wave and escalate to the
        user regardless of autonomy tier."""
        fresh = await repo.get_task(self.db, task.id)
        if fresh is not None:
            with suppress(InvalidTransition):
                await transition_task(
                    self.db, task.id, TaskStatus.DROPPED, payload={"reason": reason}
                )
        await repo.insert_event(
            self.db,
            "wave_task_dropped",
            {"task_id": task.id, "reason": reason},
            run_id=run.id,
        )
        if self.notifier is not None:
            await self.notifier.notify(
                "error",
                "Girder: wave integration escalated",
                f"run {run.id}: task {task.id} ({task.title}) dropped — {reason}",
                run_id=run.id,
            )
        return IntegrationOutcome("escalated", reason)

    @staticmethod
    def _audit_findings(audit: AuditResult) -> list[tuple[IntegrityKind, dict[str, object]]]:
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
        return findings

    async def _audit_escalation(
        self, run: Run, task: Task, audit: AuditResult
    ) -> IntegrationOutcome:
        findings = self._audit_findings(audit)
        for kind, detail in findings:
            await repo.insert_integrity_violation(
                self.db, run.id, kind.value, detail, task_id=task.id
            )
        summary = "; ".join(kind.value for kind, _ in findings) or "integration audit failed"
        return await self._drop_and_escalate(run, task, summary)

    async def _tip_audit_escalation(
        self, run: Run, audit: AuditResult
    ) -> IntegrationOutcome:
        """The wave tip (merged combination) failed the Layer 2/3 audit — the
        run branch was never moved, so there is nothing to roll back: record
        the violations and escalate like a per-merge audit failure."""
        findings = self._audit_findings(audit)
        for kind, detail in findings:
            await repo.insert_integrity_violation(self.db, run.id, kind.value, detail)
        summary = "; ".join(kind.value for kind, _ in findings) or "wave tip audit failed"
        await repo.insert_event(
            self.db,
            "wave_tip_audit_failed",
            {"reason": summary},
            run_id=run.id,
        )
        if self.notifier is not None:
            await self.notifier.notify(
                "error",
                "Girder: wave integration escalated",
                f"run {run.id}: wave tip audit failed — {summary}",
                run_id=run.id,
            )
        return IntegrationOutcome("escalated", summary)

    # git plumbing the BranchOps surface does not expose; both are plain
    # orchestrator-side ref operations (the agent has no path to them).

    async def _branch_tip(self, branch: str) -> str | None:
        return await BranchOps(self.repo_path).run_branch_tip(branch)

    async def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        proc = await run_host_cmd(
            [
                "git",
                "-C",
                str(self.repo_path),
                "merge-base",
                "--is-ancestor",
                ancestor,
                descendant,
            ],
            check=False,
            timeout_s=30,
        )
        return proc.returncode == 0

    async def _move_branch(self, branch: str, tip: str, expect: str | None = None) -> None:
        """Orchestrator-side ref move (the agent has no path to it). With
        ``expect`` this is a CAS: the move happens only from that old tip."""
        argv = ["git", "-C", str(self.repo_path), "update-ref", f"refs/heads/{branch}", tip]
        if expect is not None:
            argv.append(expect)
        await run_host_cmd(argv, check=True, timeout_s=30)
