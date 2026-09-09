"""Spec Amendment Protocol (impl-plan §6.9 amendment.py, §8.4; plan.md §8.4).

When an agent hits a mid-flight spec conflict it cannot resolve within its
scope, the orchestrator freezes the work: attempt/task/run park in their
``awaiting_amendment`` states and a pending :class:`SpecAmendment` row is
written. A human then resolves it:

* **approved** — the frozen proposal on the run branch gains an amendment
  section (committed with the same temp-worktree technique the freeze uses),
  the run's ``spec_hash`` is re-pinned to the new bytes, and the amended
  task's ``spec_slice_md`` carries the change text into its next attempt.
* **rejected** — the run resumes with the rejection guidance as a trusted
  steering directive (the TaskEngine's job to inject, not ours).
* **aborted** — task dropped, run aborted.

All status mutation goes through :mod:`girder.fsm` (status-write monopoly);
the amendment's own ``status`` is a plain CHECK column written only by
``repo.resolve_spec_amendment``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Attempt, Project, Run, RunStatus, Task, TaskStatus
from girder.fsm import InvalidTransition, transition_attempt, transition_run, transition_task
from girder.gitops.worktree import DEFAULT_BASE
from girder.notify.notifier import Notifier
from girder.specs import freeze
from girder.util import run_host_cmd

PROPOSAL_PATH = "openspec/proposals/{run_id}.md"


class AmendmentError(RuntimeError):
    """The amendment could not be requested or resolved; state is unchanged
    where possible (FSM guards reject illegal transitions before any write)."""


@dataclass(slots=True)
class ResolutionOutcome:
    decision: str
    new_spec_hash: str | None
    task_status: str
    run_status: str


# ------------------------------------------------------------------- requesting


async def request_spec_amendment(
    db: Database,
    *,
    run: Run,
    task: Task,
    attempt: Attempt,
    reason: str,
    suggested_change: str,
    notifier: Notifier | None = None,
) -> repo.SpecAmendment:
    """Park the attempt/task/run in amendment states and record the request.

    The amendment row is created first (it is the durable request record);
    the FSM transitions then guard the actual parking — an illegal state
    raises :class:`AmendmentError` via :class:`InvalidTransition`.
    """
    amendment = await repo.create_spec_amendment(
        db, run.id, reason, suggested_change, task_id=task.id
    )
    try:
        await transition_attempt(db, attempt.id, "amendment_requested")
        await transition_task(db, task.id, TaskStatus.AWAITING_AMENDMENT)
        await transition_run(db, run.id, RunStatus.AWAITING_AMENDMENT)
    except InvalidTransition as exc:
        raise AmendmentError(f"cannot request amendment for run {run.id}: {exc}") from exc

    await repo.insert_event(
        db,
        "spec_amendment_requested",
        {
            "amendment_id": amendment.id,
            "task_id": task.id,
            "attempt_id": attempt.id,
            "reason": reason,
            "suggested_change": suggested_change,
        },
        run_id=run.id,
        attempt_id=attempt.id,
    )
    if notifier is not None:
        await notifier.notify(
            "error",
            "Spec amendment requested",
            f"run {run.id} task {task.id}: {reason}",
            run_id=run.id,
        )
    return amendment


# ------------------------------------------------------------------- resolving


async def resolve_amendment(
    db: Database,
    *,
    project: Project,
    run: Run,
    amendment: repo.SpecAmendment,
    decision: str,
    guidance: str | None = None,
    notifier: Notifier | None = None,
) -> ResolutionOutcome:
    """Resolve a pending amendment: approved / rejected / aborted.

    Raises :class:`AmendmentError` if the amendment does not exist, belongs to
    another run, is already resolved, or the run is not ``awaiting_amendment``.
    """
    fresh_amendment = await repo.get_spec_amendment(db, amendment.id)
    if fresh_amendment is None:
        raise AmendmentError(f"amendment {amendment.id} not found")
    if fresh_amendment.run_id != run.id:
        raise AmendmentError(f"amendment {amendment.id} does not belong to run {run.id}")
    if fresh_amendment.status != "pending":
        raise AmendmentError(
            f"amendment {amendment.id} already resolved ({fresh_amendment.status})"
        )
    fresh_run = await repo.get_run(db, run.id)
    if fresh_run is None:
        raise AmendmentError(f"run {run.id} not found")
    if fresh_run.status != RunStatus.AWAITING_AMENDMENT:
        raise AmendmentError(
            f"run {run.id} must be awaiting_amendment to resolve an amendment"
            f" (is {fresh_run.status})"
        )

    if decision == "approved":
        outcome = await _approve(db, project=project, run=fresh_run, amendment=fresh_amendment)
    elif decision == "rejected":
        await repo.resolve_spec_amendment(db, amendment.id, status="rejected", guidance=guidance)
        run_status = await transition_run(db, run.id, RunStatus.ACTIVE)
        task_status = await _resume_task(db, fresh_amendment)
        outcome = ResolutionOutcome("rejected", None, task_status, run_status)
    elif decision == "aborted":
        await repo.resolve_spec_amendment(db, amendment.id, status="aborted")
        run_status = await transition_run(db, run.id, RunStatus.ABORTED)
        task_status = TaskStatus.DROPPED
        if fresh_amendment.task_id is not None:
            task_status = await transition_task(db, fresh_amendment.task_id, TaskStatus.DROPPED)
        outcome = ResolutionOutcome("aborted", None, str(task_status), run_status)
    else:
        raise AmendmentError(f"unknown amendment decision: {decision!r}")

    await repo.insert_event(
        db,
        "spec_amendment_resolved",
        {
            "amendment_id": amendment.id,
            "decision": decision,
            "new_spec_hash": outcome.new_spec_hash,
        },
        run_id=run.id,
    )
    if notifier is not None:
        await notifier.notify(
            "info",
            f"Spec amendment {decision}",
            f"run {run.id} amendment {amendment.id} resolved: {decision}",
            run_id=run.id,
        )
    return outcome


async def _resume_task(db: Database, amendment: repo.SpecAmendment) -> str:
    """Move the amended task back to running; no-op when the amendment is run-level."""
    if amendment.task_id is None:
        return "n/a"
    return await transition_task(db, amendment.task_id, TaskStatus.RUNNING)


async def _approve(
    db: Database, *, project: Project, run: Run, amendment: repo.SpecAmendment
) -> ResolutionOutcome:
    """Re-freeze the proposal with the amendment appended; repin the hash."""
    repo_path = Path(project.repo_path)
    path = PROPOSAL_PATH.format(run_id=run.id)
    current = await _read_branch_file(repo_path, run.branch, path)
    if current is None:
        raise AmendmentError(
            f"frozen proposal {path} missing on branch {run.branch}; cannot amend"
        )
    amended_text = (
        current
        + f"\n\n## Amendment: {amendment.id}\n\n"
        + f"**Reason:** {amendment.reason}\n\n"
        + f"**Change:** {amendment.suggested_change}\n"
    )
    new_hash = hashlib.sha256(amended_text.encode()).hexdigest()

    commit_sha = await _commit_amendment(repo_path, run.branch, run.id, amended_text)
    await repo.resolve_spec_amendment(
        db, amendment.id, status="approved", new_spec_hash=new_hash
    )
    await repo.update_run_fields(db, run.id, spec_hash=new_hash)
    if amendment.task_id is not None:
        task = await repo.get_task(db, amendment.task_id)
        if task is not None:
            await repo.update_task_fields(
                db,
                task.id,
                spec_slice_md=task.spec_slice_md + f"\n\n## Amendment: {amendment.id}\n\n"
                f"**Change:** {amendment.suggested_change}\n",
            )
    run_status = await transition_run(db, run.id, RunStatus.ACTIVE)
    task_status = await _resume_task(db, amendment)
    await repo.insert_event(
        db,
        "spec_amendment_frozen",
        {"new_spec_hash": new_hash, "commit_sha": commit_sha, "branch": run.branch},
        run_id=run.id,
    )
    return ResolutionOutcome("approved", new_hash, task_status, run_status)


async def _read_branch_file(repo_path: Path, branch: str, path: str) -> str | None:
    result = await run_host_cmd(
        ["git", "-C", str(repo_path), "show", f"{branch}:{path}"],
        check=False,
        timeout_s=30,
    )
    return result.stdout if result.returncode == 0 else None


async def _commit_amendment(
    repo_path: Path, branch: str, run_id: str, amended_text: str
) -> str:
    """Commit the amended proposal onto *branch* using freeze.py's temp-worktree
    commit technique (the branch already exists, so the base is the branch)."""
    wt_path = DEFAULT_BASE / f"amend-{run_id}"
    try:
        return await freeze._commit_in_worktree(
            repo_path, wt_path, branch, branch, run_id, amended_text
        )
    except freeze.FreezeError as exc:
        raise AmendmentError(f"failed to commit amendment for run {run_id}: {exc}") from exc
