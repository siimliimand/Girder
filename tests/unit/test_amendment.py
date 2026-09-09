"""Unit: Spec Amendment Protocol (impl-plan §6.9 amendment.py, §8.4)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Attempt, Project, Run, RunStatus, Task, TaskStatus, TaskType
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
    # knock the run out of awaiting_amendment (no legal edge back, so seed directly)
    await seed_run_status(ctx.db, ctx.run.id, RunStatus.ACTIVE.value)
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
