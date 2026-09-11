"""Conflict Fix Agent — hardened resolution of wave-integration failures.

plan.md Phase 4 task 5 / impl-plan §6.12 WP 5.4: when integrating a wave task
produces a textual git conflict (or its merge lands but breaks the suite), a
single Tier-1 capability-role agent reworks the interaction — never parallel
attempts (D5 discipline). The resolved commit is **always**, at every
autonomy tier:

1. re-tested against the FULL suite (never a subset),
2. given a dedicated, targeted conformance pass focused specifically on the
   resolution hunk (:class:`~girder.github.conformance.HunkConformanceReviewer`),
3. re-audited (Layer 2/3) against the union of the wave's task scopes —
   test files remain immutable even in resolutions.

Attempts are capped at ``limits.conflict_resolution_attempts`` (default 2).
If the cap is exhausted, an integrity violation shows up in the resolution,
or the targeted review flags any concern (or major/catastrophic), the caller
drops the task from the wave and escalates to the user regardless of tier.

The agent works in a detached worktree the orchestrator created: for a
``git_conflict`` the orchestrator pre-stages ``git merge --no-commit`` so the
agent resolves markers and completes the two-parent merge commit; for a
``semantic`` failure the agent commits a repair on top of the broken merge.
Either way the caller finds the resolution at ``ResolutionOutcome.commit``
and moves the wave branch itself.
"""

from __future__ import annotations

import asyncio
import logging
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
    Task,
)
from girder.fsm import transition_attempt
from girder.github.conformance import ConformanceError, HunkConformanceReviewer
from girder.gitops.audit import DiffAudit, TestManifest
from girder.gitops.worktree import DEFAULT_BASE, WorktreeManager
from girder.guard.redact import Redactor
from girder.guard.scope import TaskScopes
from girder.models.gateway import ModelGateway
from girder.notify.notifier import Notifier
from girder.orchestrator.suites import run_suite_in_container
from girder.sandbox.engine import ContainerSpec, SandboxEngine
from girder.stacks import STACK_REGISTRY
from girder.util import run_host_cmd, utcnow_iso

log = logging.getLogger(__name__)

# Severity bar for the targeted hunk review: none/minor pass, anything above
# fails the attempt — and ANY non-empty concerns list fails too, independent
# of severity (plan.md Phase 4 task 5: a resolution the review "flags a
# concern" on drops/escalates at every tier).
_PASSING_SEVERITIES = frozenset({"none", "minor"})


@dataclass(frozen=True)
class ResolutionOutcome:
    # resolved | unresolved | budget_exhausted
    kind: str
    commit: str | None = None
    detail: str | None = None


class ConflictResolver:
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
        hunk_reviewer: HunkConformanceReviewer,
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
        self.hunk_reviewer = hunk_reviewer
        self.worktree_base = worktree_base or DEFAULT_BASE
        self.image = f"girder-runner:{settings.project.stack}"
        self._audit = DiffAudit(
            test_signal_patterns=settings.project.test_signal_patterns,
            protected_read_paths=settings.project.protected_read_paths,
        )

    async def resolve(
        self,
        *,
        run: Run,
        task: Task,
        wave_branch: str,
        kind: str,  # "git_conflict" | "semantic"
        work_base: str,
        task_branch: str,
        failure_tail: str,
        wave_scope_globs: list[str],
    ) -> ResolutionOutcome:
        """Attempt a hardened resolution; returns the outcome and commit.

        ``work_base`` is both the checkout base of the resolution worktree and
        the CAS old-value the caller uses to move the wave branch. The caller
        owns every ref move; this module only commits in detached worktrees.
        """
        cap = self.settings.limits.conflict_resolution_attempts
        guidance: str | None = None
        last_detail = "no resolution attempts made"
        for attempt_no in range(1, cap + 1):
            outcome = await self._attempt_once(
                run=run,
                task=task,
                wave_branch=wave_branch,
                kind=kind,
                work_base=work_base,
                task_branch=task_branch,
                wave_scope_globs=wave_scope_globs,
                attempt_no=attempt_no,
                cap=cap,
                guidance=guidance,
                failure_tail=failure_tail,
            )
            if outcome[0] == "resolved":
                return ResolutionOutcome("resolved", outcome[1], outcome[2])
            if outcome[0] == "budget_exhausted":
                return ResolutionOutcome("budget_exhausted", None, outcome[2])
            if outcome[0] == "hard_failed":
                # Integrity violations never retry (§8.2) — escalate now.
                return ResolutionOutcome("unresolved", None, outcome[2])
            last_detail = outcome[2] or outcome[0]
            guidance = (
                f"conflict resolution attempt {attempt_no + 1}/{cap}. "
                f"Your previous attempt failed: {last_detail} "
                "Reconcile BOTH sides' intent and commit the result."
            )
        return ResolutionOutcome("unresolved", None, last_detail)

    # ---------------------------------------------------------------- per attempt

    async def _attempt_once(
        self,
        *,
        run: Run,
        task: Task,
        wave_branch: str,
        kind: str,
        work_base: str,
        task_branch: str,
        wave_scope_globs: list[str],
        attempt_no: int,
        cap: int,
        guidance: str | None,
        failure_tail: str,
    ) -> tuple[str, str | None, str | None]:
        """One resolution attempt. Returns (verdict, commit, detail) where
        verdict is resolved | failed | budget_exhausted | hard_failed."""
        attempt = await repo.create_attempt(self.db, task.id, work_base)
        manager = WorktreeManager(self.repo_path, self.worktree_base)
        path = await manager.create_detached(
            run_id=run.id,
            label=f"resolve-{task.id[:8]}-{attempt_no}",
            commit=work_base,
        )
        await repo.update_attempt_fields(
            self.db, attempt.id, worktree_path=str(path), started_at=utcnow_iso()
        )

        if kind == "git_conflict":
            # Pre-stage the merge: the agent resolves the markers and commits,
            # producing the two-parent merge commit itself.
            proc = await run_host_cmd(
                ["git", "-C", str(path), "merge", "--no-ff", "--no-commit", task_branch],
                check=False,
                timeout_s=60,
            )
            if proc.returncode not in (0, 1):
                await self._close_failed(attempt, f"merge failed: {proc.stderr[:300]}")
                with suppress(Exception):
                    await manager.remove(path, force=True)
                return ("failed", None, f"merge failed: {proc.stderr.strip()[:200]}")

        manifest = await self._audit.capture_test_manifest(path)
        # Forward the operator's [sandbox] policy (cf. task_engine._container_spec).
        spec = ContainerSpec(
            name=f"girder-{attempt.id[:8]}",
            image=self.image,
            worktree=path,
            network=self.settings.sandbox.network,
            memory=self.settings.sandbox.memory,
            cpus=self.settings.sandbox.cpus,
            pids_limit=self.settings.sandbox.pids_limit,
        )
        await self.sandbox.start(spec)
        await repo.update_attempt_fields(self.db, attempt.id, container_id=spec.name)
        await transition_attempt(self.db, attempt.id, AttemptStatus.RUNNING)

        try:
            if guidance is None:
                guidance = self._base_guidance(kind, failure_tail, attempt_no, cap)
            runtime = AgentRuntime(
                gateway=self.gateway,
                sandbox=self.sandbox,
                container=spec.name,
                scopes=TaskScopes(
                    write_globs=wave_scope_globs,
                    protected_globs=self.settings.project.protected_read_paths,
                    strict_read_scope=self.settings.project.strict_read_scope,
                ),
                limits=self.settings.limits,
                python_bin=self.settings.sandbox.python_bin,
                redactor=self.redactor,
                db=self.db,
                run_id=run.id,
                attempt=attempt,
                task=task,
                stack=STACK_REGISTRY[self.settings.project.stack],
                model_role="tier1",
            )
            try:
                outcome = await runtime.execute_attempt(
                    spec_slice=task.spec_slice_md,
                    guidance=guidance,
                    deadline_s=asyncio.get_running_loop().time()
                    + self.settings.limits.attempt_wallclock_s,
                )
            except BudgetExceeded:
                return ("budget_exhausted", None, "budget exhausted during resolution")

            if outcome.status == "amendment_requested":
                await self._close(
                    attempt, AttemptStatus.AMENDMENT_REQUESTED, "amendment during resolution"
                )
                return ("failed", None, "spec amendment requested during resolution")
            if outcome.status != "succeeded":
                detail = self._redact(outcome.failure_reason or outcome.summary or "")
                await self._close_failed(attempt, f"agent {outcome.status}: {detail}")
                return ("failed", None, f"agent {outcome.status}: {detail}")

            head = await self._rev_parse(path)
            if head == work_base:
                await self._close_failed(attempt, "no resolution commit was made")
                return ("failed", None, "no resolution commit was made")
            if kind == "git_conflict" and not await self._is_merge_commit(head):
                await self._close_failed(
                    attempt, "resolution must complete the two-parent merge commit"
                )
                return ("failed", None, "resolution must complete the merge commit")

            # Mandatory gate 1: FULL suite re-test (plan.md Phase 4 task 5).
            suite = await run_suite_in_container(
                self.sandbox,
                spec.name,
                path,
                self.redactor,
                allow_empty_baseline=project_allows_empty_baseline(self.repo_path),
            )
            if not suite.green:
                await self._close_failed(attempt, f"suite red after resolution: {suite.tail}")
                return ("failed", None, f"suite still red ({suite.test_count} tests)")

            # Mandatory gate 2: Layer 2/3 audit over the union scope — test
            # files stay immutable even in a resolution (§8.2 at every tier).
            audit = await self._audit.audit_commit(
                self.repo_path,
                task_type=task.task_type,
                scope_globs=wave_scope_globs,
                base_commit=work_base,
                commit=head,
                start_manifest=TestManifest(root_hash=manifest.root_hash, files={}),
            )
            if not audit.passed:
                summary = self._audit_summary(audit)
                violation_kind = (
                    IntegrityKind.TEST_PATH_MODIFIED
                    if audit.test_path_violations or audit.content_hash_mismatches
                    else IntegrityKind.SCOPE_VIOLATION
                )
                await repo.insert_integrity_violation(
                    self.db,
                    run.id,
                    violation_kind.value,
                    {"audit": summary, "commit": head},
                    task_id=task.id,
                    attempt_id=attempt.id,
                )
                await self._close(attempt, AttemptStatus.INTEGRITY_VIOLATION, summary)
                # Integrity violations are never retried (§8.2).
                return ("hard_failed", None, f"integrity violation in resolution: {summary}")

            # Mandatory gate 3: targeted Tier-1 review of the resolution hunk.
            hunk = await run_host_cmd(
                ["git", "-C", str(self.repo_path), "diff", work_base, head],
                check=True,
                timeout_s=60,
            )
            context = (
                f"Task {task.id} ({task.title}) was integrated into {wave_branch} "
                f"and {kind} had to be resolved. Judge whether the resolution "
                f"reconciles both integrated sides without dropping either's "
                f"declared behavior."
            )
            try:
                verdict = await self.hunk_reviewer.review(
                    run=run, hunk_diff=hunk.stdout, context=context
                )
            except ConformanceError as exc:
                await self._close_failed(attempt, f"hunk review unparseable: {exc}")
                return ("failed", None, f"hunk review failed to produce a verdict: {exc}")

            # Spec-strict gate (plan.md Phase 4 task 5): a verdict that flags
            # ANY concern makes the resolution unsound regardless of severity;
            # the severity bar remains an independent floor.
            unsound = (not verdict.resolution_sound) or bool(verdict.concerns)
            if unsound or verdict.severity not in _PASSING_SEVERITIES:
                detail = self._redact(
                    f"severity={verdict.severity}: {verdict.summary} "
                    f"concerns={'; '.join(verdict.concerns)}"
                )
                await self._close_failed(attempt, f"hunk review flagged: {detail}")
                return ("failed", None, f"targeted conformance flagged the resolution: {detail}")

            await transition_attempt(self.db, attempt.id, AttemptStatus.SUCCEEDED)
            await repo.update_attempt_fields(
                self.db, attempt.id, ended_at=utcnow_iso(), failure_reason=None
            )
            await repo.insert_event(
                self.db,
                "conflict_resolved",
                {
                    "task_id": task.id,
                    "kind": kind,
                    "commit": head,
                    "attempt_no": attempt_no,
                    "severity": verdict.severity,
                },
                run_id=run.id,
                attempt_id=attempt.id,
            )
            return ("resolved", head, verdict.summary)
        finally:
            with suppress(Exception):
                await self.sandbox.kill(spec.name)
            with suppress(Exception):
                await manager.remove(path, force=True)

    # ------------------------------------------------------------------ helpers

    def _base_guidance(self, kind: str, failure_tail: str, attempt_no: int, cap: int) -> str:
        if kind == "git_conflict":
            situation = (
                "A merge is in progress with conflicts — resolve every conflict "
                "marker in the working tree, `git add` the resolved files, and "
                "`git commit` to complete the two-parent merge commit. Do not "
                "drop either side's behavior."
            )
        else:
            situation = (
                "The merged state is committed but the test suite fails — "
                "diagnose the interaction between the merged changes and commit "
                "a repair on top (a normal commit)."
            )
        return (
            f"conflict resolution attempt {attempt_no}/{cap}. {situation}\n"
            f"Failure context (redacted):\n{self._redact(failure_tail)}\n"
            "Work only within the declared scope; test files are read-only. "
            "Finish with mark_task_complete."
        )

    def _audit_summary(self, audit_result: object) -> str:
        parts = []
        for attr in (
            "test_path_violations",
            "content_hash_mismatches",
            "out_of_scope_writes",
            "protected_path_touches",
            "uncommitted_leftovers",
        ):
            paths = getattr(audit_result, attr, None)
            if paths:
                parts.append(f"{attr}: {', '.join(paths[:5])}")
        return "; ".join(parts)

    async def _close_failed(self, attempt: Attempt, reason: str) -> None:
        await self._close(attempt, AttemptStatus.FAILED, reason)

    async def _close(self, attempt: Attempt, status: AttemptStatus, reason: str) -> None:
        with suppress(Exception):
            await transition_attempt(self.db, attempt.id, status)
        await repo.update_attempt_fields(
            self.db, attempt.id, failure_reason=self._redact(reason), ended_at=utcnow_iso()
        )

    def _redact(self, text: str) -> str:
        return self.redactor.redact(text)[0]

    async def _rev_parse(self, worktree_path: Path) -> str:
        proc = await run_host_cmd(
            ["git", "-C", str(worktree_path), "rev-parse", "HEAD"], timeout_s=30
        )
        return proc.stdout.strip()

    async def _is_merge_commit(self, commit: str) -> bool:
        proc = await run_host_cmd(
            ["git", "-C", str(self.repo_path), "rev-list", "--parents", "-n", "1", commit],
            timeout_s=30,
        )
        return len(proc.stdout.split()) >= 3
