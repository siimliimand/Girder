"""Verification loop: in-container test run, diff audit, and integration.

Cut from :mod:`girder.orchestrator.task_engine` (WP 10.2). Runs the suite via
the stack plugin (``STACK_REGISTRY`` — WS-07), enforces the Layer 2/3
mechanical integrity audit, and either ff-only merges the attempt onto the
run branch or parks it at ``verify_passed`` (wave mode).

Integrity violations (plan §8.2) fail WITHOUT retry and never merge.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from girder.config import project_allows_empty_baseline
from girder.db import repo
from girder.db.models import (
    Attempt,
    AttemptStatus,
    IntegrityKind,
    Task,
    TaskStatus,
)
from girder.fsm import transition_attempt, transition_task
from girder.gitops.audit import AuditResult
from girder.gitops.branch import BranchOps
from girder.guard.redact import redact_and_log
from girder.orchestrator.attempt_lifecycle import (
    AttemptContext,
    close_attempt,
    fail_task,
    kill_attempt_container,
    retry_or_fail,
)
from girder.orchestrator.baseline import empty_suite_accepted, parse_junit_xml
from girder.orchestrator.work_salvage import salvage_worktree
from girder.stacks import STACK_REGISTRY, VERIFY_XML
from girder.util import run_host_cmd, utcnow_iso

if TYPE_CHECKING:
    from girder.db.models import Run
    from girder.orchestrator.task_engine import TaskEngine

# Keep the historical logger name (tests assert on it via caplog).
log = logging.getLogger("girder.orchestrator.task_engine")

# Compatibility re-export: the verify command now lives in the stack plugin
# (WS-07); orchestrator.suites still imports VERIFY_CMD from here (via
# task_engine).
VERIFY_CMD = STACK_REGISTRY["python-3.12"].test_command()
_VERIFY_TIMEOUT_S = 900.0
_TAIL_CHARS = 2000


@dataclass(frozen=True)
class VerificationResult:
    """Internal: completed/verified/failed/integrity_violation, or retry."""

    kind: str
    detail: str | None
    # no_changes=true declared by the agent (Group D — empty-diff honesty).
    no_changes: bool = False
    # (salvage sha, changed files) when the failed attempt's dirty worktree
    # was committed to the task branch before teardown (Group B).
    salvage: tuple[str, list[str]] | None = None


# Historical internal alias (pre-split name).
_VerifyStep = VerificationResult


def redact_tail(engine: TaskEngine, text: str) -> str:
    redacted, _ = engine.redactor.redact(text[-_TAIL_CHARS:])
    return redacted


async def run_verification(
    engine: TaskEngine,
    run: Run,
    task: Task,
    ctx: AttemptContext,
    base_commit: str,
    *,
    integrate: bool = True,
    agent_no_changes: bool = False,
    turns_used: int | None = None,
) -> VerificationResult:
    """Verify the attempt's diff and integrate it (merge or wave-mode park)."""
    attempt = ctx.attempt
    worktree = ctx.worktree
    manifest = ctx.manifest
    container = ctx.container_id
    await transition_task(engine.db, task.id, TaskStatus.VERIFYING)
    # §8 (impl-plan): integrity violations block the merge at every tier —
    # ANY held/scope-violating tool call taints this attempt, even when the
    # terminal turn itself was clean (runtime.py only catches the
    # same-turn case). The ledger rows were already written by the tool
    # registry (exactly once); fail the attempt without retry here.
    held_count = await repo.count_held_tool_calls(engine.db, attempt.id)
    if held_count:
        reason = f"{held_count} held scope-violating tool call(s) on attempt {attempt.id}"
        await fail_without_retry(engine, run, task, attempt, reason)
        return VerificationResult("integrity_violation", reason)
    exec_res = await engine.sandbox.exec(
        container,
        STACK_REGISTRY[engine.settings.project.stack].test_command(
            engine.settings.sandbox.python_bin
        ),
        timeout_s=_VERIFY_TIMEOUT_S,
    )
    xml_path = worktree.path / VERIFY_XML
    allow_empty = project_allows_empty_baseline(engine.repo_path)
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
        tail = redact_tail(engine, exec_res.stdout + "\n" + exec_res.stderr)
        return await retry_step(
            engine,
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
    await kill_attempt_container(engine, container)

    # Layer 2/3 mechanical audit of the observed diff.
    audit = await engine._audit.audit_attempt(
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
        return await retry_step(
            engine,
            run,
            task,
            attempt,
            detail,
            event="verify_failed",
            note="uncommitted leftovers",
            turns_used=turns_used,
        )
    if not audit.passed:
        return await integrity_violation(engine, run, task, attempt, audit)

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
        return await retry_step(
            engine,
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
            str(engine.repo_path),
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
        engine.redactor,
        diff_res.stdout,
        source_field="attempt_diff",
        db=engine.db,
        attempt_id=attempt.id,
    )
    await repo.insert_attempt_diff(
        engine.db,
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
        await transition_attempt(engine.db, attempt.id, AttemptStatus.SUCCEEDED)
        if turns_used is not None:
            await repo.update_attempt_fields(engine.db, attempt.id, turns_used=turns_used)
        await repo.insert_event(
            engine.db,
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
        await transition_task(engine.db, task.id, TaskStatus.VERIFY_PASSED)
        return VerificationResult("verified", head.stdout.strip(), no_changes=agent_no_changes)
    merge = await BranchOps(engine.repo_path).audit_gated_merge(
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
        return await retry_step(
            engine,
            run,
            task,
            attempt,
            detail,
            event="merge_refused",
            note=detail,
            turns_used=turns_used,
        )

    await transition_attempt(engine.db, attempt.id, AttemptStatus.SUCCEEDED)
    if turns_used is not None:
        await repo.update_attempt_fields(engine.db, attempt.id, turns_used=turns_used)
    await repo.insert_event(
        engine.db,
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
    await transition_task(engine.db, task.id, TaskStatus.VERIFY_PASSED)
    await transition_task(engine.db, task.id, TaskStatus.COMPLETED)
    return VerificationResult("completed", merge.merged_commit, no_changes=agent_no_changes)


async def integrity_violation(
    engine: TaskEngine,
    run: Run,
    task: Task,
    attempt: Attempt,
    audit: AuditResult,
) -> VerificationResult:
    # §8.2: integrity violations fail WITHOUT retry and never merge. Only
    # the five mechanical kinds count — uncommitted leftovers are handled
    # upstream as an ordinary retryable failure.
    findings: list[tuple[IntegrityKind, dict[str, object]]] = []
    if audit.test_path_violations:
        findings.append((IntegrityKind.TEST_PATH_MODIFIED, {"paths": audit.test_path_violations}))
    if audit.content_hash_mismatches:
        findings.append(
            (IntegrityKind.CONTENT_HASH_MISMATCH, {"paths": audit.content_hash_mismatches})
        )
    if audit.out_of_scope_writes:
        findings.append((IntegrityKind.OUT_OF_SCOPE_WRITE, {"paths": audit.out_of_scope_writes}))
    if audit.protected_path_touches:
        findings.append((IntegrityKind.PROTECTED_READ, {"paths": audit.protected_path_touches}))
    if not findings:  # pragma: no cover - guarded by the callers above
        raise RuntimeError("integrity violation with no mechanical findings")
    for kind, detail in findings:
        await repo.insert_integrity_violation(
            engine.db, run.id, kind.value, detail, task_id=task.id, attempt_id=attempt.id
        )
    summary = "; ".join(kind.value for kind, _ in findings)
    await fail_without_retry(engine, run, task, attempt, summary)
    return VerificationResult("integrity_violation", summary)


async def fail_without_retry(
    engine: TaskEngine, run: Run, task: Task, attempt: Attempt, reason: str
) -> None:
    await transition_attempt(engine.db, attempt.id, AttemptStatus.INTEGRITY_VIOLATION)
    await repo.update_attempt_fields(
        engine.db, attempt.id, failure_reason=reason, ended_at=utcnow_iso()
    )
    await fail_task(engine, task, reason)
    if engine.notifier is not None:
        await engine.notifier.notify(
            "error",
            "Girder: integrity violation — task failed without retry",
            f"run {run.id} task {task.id}: {reason}",
            run_id=run.id,
        )


async def retry_step(
    engine: TaskEngine,
    run: Run,
    task: Task,
    attempt: Attempt,
    detail: str,
    *,
    event: str,
    note: str,
    turns_used: int | None = None,
) -> VerificationResult:
    """Close the attempt FAILED and retry with feedback, or fail the task."""
    # Group B: salvage any uncommitted work before the engine-level
    # finally prunes the worktree — verify-fail retries are ordinary
    # retries, so the same salvage semantics apply.
    salvage: tuple[str, list[str]] | None = None
    with suppress(Exception):
        salvage = await salvage_worktree(engine.repo_path, task, attempt)
    if salvage is not None:
        engine._salvage_tips[task.id] = salvage[0]
    tail = redact_tail(engine, detail)
    await close_attempt(
        engine, attempt.id, AttemptStatus.FAILED.value, tail, turns_used=turns_used
    )
    next_guidance = await retry_or_fail(engine, run, task, tail, event=event, note=note)
    if next_guidance is None:
        return VerificationResult("failed", tail)
    # Feedback to the next attempt must be the redacted tail (Phase 2
    # task 5): the raw detail can carry git stderr with secret-shaped
    # content (e.g. a merge-refusal echoing a token).
    return VerificationResult("retry", tail, salvage=salvage)
