"""Unit: Spec Amendment Protocol (impl-plan §6.9 amendment.py, §8.4)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from girder.db import repo
from girder.db.engine import Database
from girder.db.models import (
    Attempt,
    AttemptStatus,
    Project,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    TaskType,
)
from girder.fsm import transition_attempt, transition_run, transition_task
from girder.specs.amendment import AmendmentError, request_spec_amendment, resolve_amendment
from girder.specs.freeze import approve_and_freeze
from girder.util import run_host_cmd
from tests.conftest import seed_run_status

pytestmark = pytest.mark.integration  # needs a real git repo for the approve path

PROPOSAL = """\
---
schema: girder.openspec/v1
title: Amendment test
intent: Verify the amendment path end to end.
tasks:
  - id: do-thing
    title: Do the thing
    type: code_change
    scope_globs: ["src/**"]
    success_criteria: ["the thing is done"]
    depends_on: []
---
Narrative.
"""


async def _git(cwd: Path, *args: str) -> str:
    result = await run_host_cmd(["git", "-C", str(cwd), *args], timeout_s=30)
    return result.stdout


@pytest.fixture
async def git_repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    await _git(repo_dir, "init", "-b", "main")
    await _git(repo_dir, "config", "user.email", "test@girder.local")
    await _git(repo_dir, "config", "user.name", "girder-test")
    (repo_dir / "README.md").write_text("repo\n")
    await _git(repo_dir, "add", "-A")
    await _git(repo_dir, "commit", "-m", "initial")
    return repo_dir


@dataclass
class Ctx:
    db: Database
    project: Project
    run: Run
    task: Task
    attempt: Attempt
    git_repo: Path
    old_hash: str


@pytest.fixture
async def ctx(db: Database, git_repo: Path, tmp_path: Path) -> Ctx:
    """A frozen run with one running task + attempt, ready to request an amendment."""
    project = await repo.create_project(db, "amend-test", str(git_repo))
    run = await repo.create_run(db, project.id, "intent", "run/amend1", 5.0)
    await seed_run_status(db, run.id, RunStatus.SPEC_PENDING.value)
    fresh = await repo.get_run(db, run.id)
    assert fresh is not None
    result = await approve_and_freeze(
        db, project=project, run=fresh, proposal_text=PROPOSAL,
        repo_path=git_repo, worktree_base=tmp_path / "wt",
    )
    await transition_run(db, run.id, RunStatus.BASELINE_RUNNING)
    await transition_run(db, run.id, RunStatus.ACTIVE)
    wave = await repo.get_or_create_wave0(db, run.id)
    task = await repo.create_task(
        db, wave.id, 1, "Do the thing", TaskType.CODE_CHANGE,
        scope_globs=["src/**"], spec_slice_md="## Task: do-thing\n",
    )
    await transition_task(db, task.id, TaskStatus.SCHEDULED)
    await transition_task(db, task.id, TaskStatus.RUNNING)
    attempt = await repo.create_attempt(db, task.id, result.commit_sha)
    await transition_attempt(db, attempt.id, "running")
    task_fresh = await repo.get_task(db, task.id)
    attempt_fresh = await repo.get_attempt(db, attempt.id)
    run_fresh = await repo.get_run(db, run.id)
    assert task_fresh is not None and attempt_fresh is not None and run_fresh is not None
    return Ctx(
        db=db, project=project, run=run_fresh, task=task_fresh,
        attempt=attempt_fresh, git_repo=git_repo, old_hash=result.spec_hash,
    )


async def _request(ctx: Ctx) -> repo.SpecAmendment:
    return await request_spec_amendment(
        ctx.db,
        run=ctx.run,
        task=ctx.task,
        attempt=ctx.attempt,
        reason="spec slice is ambiguous",
        suggested_change="add explicit acceptance note to do-thing",
    )


async def seed_task_status(db: Database, task_id: str, status: str) -> None:
    """Deliberately bypasses the FSM — test-only seeding of a task's status."""
    await db.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
    await db.conn.commit()


@dataclass
class _FakeNotifier:
    """Captures notification bodies (§8.4: payload must carry the diff)."""

    bodies: list[str]

    async def notify(
        self, level: str, title: str, body: str, *, run_id: str | None = None
    ) -> list[object]:
        self.bodies.append(body)
        return []


async def test_request_notification_includes_suggested_change(ctx: Ctx) -> None:
    notifier = _FakeNotifier(bodies=[])
    long_change = "add explicit acceptance note to do-thing " + "x" * 600
    await request_spec_amendment(
        ctx.db,
        run=ctx.run,
        task=ctx.task,
        attempt=ctx.attempt,
        reason="spec slice is ambiguous",
        suggested_change=long_change,
        notifier=notifier,  # type: ignore[arg-type]
    )
    assert len(notifier.bodies) == 1
    body = notifier.bodies[0]
    assert ctx.run.id in body and ctx.task.id in body
    assert "spec slice is ambiguous" in body
    assert "add explicit acceptance note to do-thing" in body
    assert "x" * 600 not in body  # truncated for chat-sized channels
    assert "[...truncated]" in body


async def test_request_parks_everything(ctx: Ctx) -> None:
    amendment = await _request(ctx)
    assert amendment.status == "pending"
    assert amendment.task_id == ctx.task.id
    fresh_run = await repo.get_run(ctx.db, ctx.run.id)
    fresh_task = await repo.get_task(ctx.db, ctx.task.id)
    assert fresh_run is not None and fresh_run.status is RunStatus.AWAITING_AMENDMENT
    assert fresh_task is not None and fresh_task.status is TaskStatus.AWAITING_AMENDMENT
    events = await ctx.db.fetchall(
        "SELECT event_type FROM agent_events WHERE run_id = ? AND event_type ="
        " 'spec_amendment_requested'",
        (ctx.run.id,),
    )
    assert events


async def test_approve_refreezes_and_repins(ctx: Ctx) -> None:
    amendment = await _request(ctx)
    outcome = await resolve_amendment(
        ctx.db, project=ctx.project, run=ctx.run, amendment=amendment, decision="approved"
    )
    assert outcome.decision == "approved"
    assert outcome.new_spec_hash != ctx.old_hash
    assert outcome.task_status == "running"
    assert outcome.run_status == "active"

    fresh_run = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh_run is not None
    assert fresh_run.spec_hash == outcome.new_spec_hash

    blob = await _git(
        ctx.git_repo, "show", f"{ctx.run.branch}:openspec/proposals/{ctx.run.id}.md"
    )
    assert "## Amendment:" in blob
    assert amendment.id in blob
    assert hashlib.sha256(blob.encode()).hexdigest() == outcome.new_spec_hash

    fresh_task = await repo.get_task(ctx.db, amendment.task_id)
    assert fresh_task is not None
    assert fresh_task.status is TaskStatus.RUNNING
    assert "add explicit acceptance note" in fresh_task.spec_slice_md


async def test_reject_returns_to_active_with_guidance(ctx: Ctx) -> None:
    amendment = await _request(ctx)
    outcome = await resolve_amendment(
        ctx.db,
        project=ctx.project,
        run=ctx.run,
        amendment=amendment,
        decision="rejected",
        guidance="proceed without the change",
    )
    assert outcome.decision == "rejected"
    assert outcome.new_spec_hash is None
    assert outcome.run_status == "active"
    assert outcome.task_status == "running"
    stored = await repo.get_spec_amendment(ctx.db, amendment.id)
    assert stored is not None
    assert stored.status == "rejected"
    assert stored.guidance == "proceed without the change"
    fresh_run = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh_run is not None
    assert fresh_run.spec_hash == ctx.old_hash  # untouched
    # the TaskEngine's lookup relays the guidance on resume (impl-plan §6.9)
    assert amendment.task_id is not None
    found = await repo.get_rejected_guidance(ctx.db, ctx.run.id, amendment.task_id)
    assert found == "proceed without the change"


async def test_get_rejected_guidance_none_without_rejection(ctx: Ctx) -> None:
    amendment = await _request(ctx)
    assert amendment.task_id is not None
    assert await repo.get_rejected_guidance(ctx.db, ctx.run.id, amendment.task_id) is None
    # a pending amendment (no guidance) still yields None
    await resolve_amendment(
        ctx.db, project=ctx.project, run=ctx.run, amendment=amendment, decision="approved"
    )
    assert await repo.get_rejected_guidance(ctx.db, ctx.run.id, amendment.task_id) is None


async def test_abort_drops_task_and_aborts_run(ctx: Ctx) -> None:
    amendment = await _request(ctx)
    outcome = await resolve_amendment(
        ctx.db, project=ctx.project, run=ctx.run, amendment=amendment, decision="aborted"
    )
    assert outcome.task_status == "dropped"
    assert outcome.run_status == "aborted"
    fresh_run = await repo.get_run(ctx.db, ctx.run.id)
    fresh_task = await repo.get_task(ctx.db, amendment.task_id)
    assert fresh_run is not None and fresh_run.status is RunStatus.ABORTED
    assert fresh_task is not None and fresh_task.status is TaskStatus.DROPPED


async def test_double_resolution_refused(ctx: Ctx) -> None:
    amendment = await _request(ctx)
    await resolve_amendment(
        ctx.db, project=ctx.project, run=ctx.run, amendment=amendment, decision="rejected"
    )
    with pytest.raises(AmendmentError, match="already resolved"):
        await resolve_amendment(
            ctx.db, project=ctx.project, run=ctx.run, amendment=amendment, decision="approved"
        )


async def test_wrong_run_refused(ctx: Ctx) -> None:
    amendment = await _request(ctx)
    other = await repo.create_run(ctx.db, ctx.project.id, "other", "run/other", 5.0)
    with pytest.raises(AmendmentError, match="does not belong"):
        await resolve_amendment(
            ctx.db, project=ctx.project, run=other, amendment=amendment, decision="approved"
        )


async def test_run_not_awaiting_refused(ctx: Ctx) -> None:
    amendment = await _request(ctx)
    # knock the run out of awaiting_amendment into a non-recoverable state (no
    # legal edge back, so seed directly). ACTIVE with a pending amendment IS
    # recoverable now (crash-window path below) — FAILED is not.
    await seed_run_status(ctx.db, ctx.run.id, RunStatus.FAILED.value)
    with pytest.raises(AmendmentError, match="awaiting_amendment"):
        await resolve_amendment(
            ctx.db, project=ctx.project, run=ctx.run, amendment=amendment, decision="approved"
        )


async def test_unknown_decision(ctx: Ctx) -> None:
    amendment = await _request(ctx)
    with pytest.raises(AmendmentError, match="unknown amendment decision"):
        await resolve_amendment(
            ctx.db, project=ctx.project, run=ctx.run, amendment=amendment, decision="maybe"
        )


async def test_failed_parking_does_not_leave_pending_row(ctx: Ctx) -> None:
    """When the FSM parking transitions fail, the amendment row must not stay
    'pending' — get_pending_amendment would otherwise freeze the run pump on a
    phantom amendment (§8.4). The row is kept as 'aborted' for the audit trail."""
    # Make the run transition illegal (aborted runs cannot be parked).
    await seed_run_status(ctx.db, ctx.run.id, RunStatus.ABORTED.value)
    with pytest.raises(AmendmentError, match="cannot request amendment"):
        await _request(ctx)
    rows = await repo.list_amendments_for_run(ctx.db, ctx.run.id)
    assert len(rows) == 1
    assert rows[0].status == "aborted"
    assert await repo.get_pending_amendment(ctx.db, ctx.run.id) is None


# ---------------------------------------------------- crash-window recovery (D1)


async def _crash_window_amendment(ctx: Ctx) -> repo.SpecAmendment:
    """Simulate the crash: the durable amendment row is written BEFORE the
    FSM parking, and the process dies between the two — the run stays ACTIVE
    and the task stays RUNNING with a pending amendment."""
    return await repo.create_spec_amendment(
        ctx.db, ctx.run.id, "spec slice is ambiguous", "fix it", task_id=ctx.task.id
    )


@pytest.mark.parametrize("decision", ["approved", "rejected", "aborted"])
async def test_resolve_from_active_crash_window(ctx: Ctx, decision: str) -> None:
    """§8.7: a run left ACTIVE with a pending amendment must not be wedged —
    Approve/Reject/Abort all stay reachable; resolution finishes the parking
    itself."""
    amendment = await _crash_window_amendment(ctx)
    outcome = await resolve_amendment(
        ctx.db, project=ctx.project, run=ctx.run, amendment=amendment, decision=decision
    )
    assert outcome.decision == decision
    fresh_run = await repo.get_run(ctx.db, ctx.run.id)
    assert fresh_run is not None
    if decision == "aborted":
        assert fresh_run.status is RunStatus.ABORTED
    else:
        assert fresh_run.status is RunStatus.ACTIVE


async def test_crash_window_approve_repins_and_resumes(ctx: Ctx) -> None:
    amendment = await _crash_window_amendment(ctx)
    outcome = await resolve_amendment(
        ctx.db, project=ctx.project, run=ctx.run, amendment=amendment, decision="approved"
    )
    fresh_run = await repo.get_run(ctx.db, ctx.run.id)
    fresh_task = await repo.get_task(ctx.db, ctx.task.id)
    assert fresh_run is not None and fresh_task is not None
    assert fresh_run.spec_hash == outcome.new_spec_hash
    assert fresh_task.status is TaskStatus.RUNNING


# ------------------------------------------------------ scope widening (D2, M5)


async def test_approve_with_scope_globs_unions_task_scope(ctx: Ctx) -> None:
    """Approving with scope_globs unions them into tasks.scope_globs_json
    (§8.4: approval may widen scope; it never narrows)."""
    amendment = await _request(ctx)
    await resolve_amendment(
        ctx.db,
        project=ctx.project,
        run=ctx.run,
        amendment=amendment,
        decision="approved",
        scope_globs=["docs/**", "src/**"],  # src/** already authorized -> dedup
    )
    fresh_task = await repo.get_task(ctx.db, ctx.task.id)
    assert fresh_task is not None
    assert fresh_task.scope_globs == ["src/**", "docs/**"]
    stored = await repo.get_spec_amendment(ctx.db, amendment.id)
    assert stored is not None
    assert stored.scope_globs == ["docs/**", "src/**"]


async def test_reject_ignores_scope_globs(ctx: Ctx) -> None:
    amendment = await _request(ctx)
    await resolve_amendment(
        ctx.db,
        project=ctx.project,
        run=ctx.run,
        amendment=amendment,
        decision="rejected",
        scope_globs=["docs/**"],
    )
    fresh_task = await repo.get_task(ctx.db, ctx.task.id)
    assert fresh_task is not None
    assert fresh_task.scope_globs == ["src/**"]
    stored = await repo.get_spec_amendment(ctx.db, amendment.id)
    assert stored is not None
    assert stored.scope_globs == []


async def test_invalid_scope_glob_refused_and_stays_pending(ctx: Ctx) -> None:
    amendment = await _request(ctx)
    with pytest.raises(AmendmentError, match="must not contain"):
        await resolve_amendment(
            ctx.db,
            project=ctx.project,
            run=ctx.run,
            amendment=amendment,
            decision="approved",
            scope_globs=["../etc"],
        )
    stored = await repo.get_spec_amendment(ctx.db, amendment.id)
    assert stored is not None
    assert stored.status == "pending"
    fresh_task = await repo.get_task(ctx.db, ctx.task.id)
    assert fresh_task is not None
    assert fresh_task.scope_globs == ["src/**"]


@pytest.mark.parametrize(
    "bad_glob", ["/abs/path", "../up", "a/../../b", ""]  # type: ignore[list-item]
)
async def test_scope_glob_rules_match_validator(ctx: Ctx, bad_glob: str) -> None:
    amendment = await _request(ctx)
    with pytest.raises(AmendmentError):
        await resolve_amendment(
            ctx.db,
            project=ctx.project,
            run=ctx.run,
            amendment=amendment,
            decision="approved",
            scope_globs=[bad_glob],
        )
    stored = await repo.get_spec_amendment(ctx.db, amendment.id)
    assert stored is not None
    assert stored.status == "pending"


async def test_request_persists_scope_globs(ctx: Ctx) -> None:
    amendment = await request_spec_amendment(
        ctx.db,
        run=ctx.run,
        task=ctx.task,
        attempt=ctx.attempt,
        reason="need the docs dir too",
        suggested_change="widen scope",
        scope_globs=["docs/**"],
    )
    stored = await repo.get_spec_amendment(ctx.db, amendment.id)
    assert stored is not None
    assert stored.scope_globs == ["docs/**"]


# -------------------------------------------- partial-park residue (D3, L4 test)


async def test_parking_failure_leaves_no_amendment_requested_residue(ctx: Ctx) -> None:
    """§8.7: if a mid-park transition fails after the attempt was parked, no
    attempt may stay stranded in the non-terminal amendment_requested state.
    Parking is ordered run -> task -> attempt with compensation, so a failure
    leaves the attempt untouched and reverses any parked task/run."""
    # Make the task parking illegal (completed tasks cannot be parked) — the
    # run parks first, then the failure hits.
    await seed_task_status(ctx.db, ctx.task.id, TaskStatus.COMPLETED.value)
    with pytest.raises(AmendmentError, match="cannot request amendment"):
        await _request(ctx)
    fresh_run = await repo.get_run(ctx.db, ctx.run.id)
    fresh_task = await repo.get_task(ctx.db, ctx.task.id)
    fresh_attempt = await repo.get_attempt(ctx.db, ctx.attempt.id)
    assert fresh_run is not None and fresh_run.status is RunStatus.ACTIVE  # reversed
    assert fresh_task is not None and fresh_task.status is TaskStatus.COMPLETED
    assert fresh_attempt is not None
    assert fresh_attempt.status is not AttemptStatus.AMENDMENT_REQUESTED
    assert await repo.get_pending_amendment(ctx.db, ctx.run.id) is None
