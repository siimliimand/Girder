"""Integration: crash recovery + pump resumption (plan.md §8.7 / SC-06).

Simulates a crash mid-task: a `running` attempt with a live worktree row and a
dirty worktree directory, next to an already-COMPLETED task. Recovery must
crash the attempt, reschedule the task, prune the worktree — and the next pump
must execute exactly one fresh attempt from a clean base commit without
re-executing the completed task.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from girder.config import LimitsConfig, ProjectConfig, SandboxNetwork, Secrets, Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import AttemptStatus, RunStatus, TaskStatus, TaskType, WorktreeState
from girder.fsm import transition_attempt
from girder.gitops.worktree import WorktreeManager
from girder.guard.redact import Redactor
from girder.orchestrator.recovery import RecoveryService
from girder.orchestrator.run_engine import RunEngine
from girder.sandbox.engine import ExecResult
from girder.util import run_host_cmd
from tests.conftest import seed_run_status
from tests.unit.test_task_engine import (
    GREEN_XML,
    FakeGateway,
    ScriptSandbox,
    write_commit_complete,
)

pytestmark = pytest.mark.integration


async def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = await run_host_cmd(["git", "-C", str(cwd), *args], check=check, timeout_s=30)
    return result.stdout


@pytest.fixture
async def seeded(db: Database, tmp_path: Path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    await _git(repo_path, "init", "-b", "main")
    await _git(repo_path, "config", "user.email", "test@girder.local")
    await _git(repo_path, "config", "user.name", "girder-test")
    (repo_path / "src").mkdir()
    (repo_path / "src" / "lib.py").write_text("x = 1\n")
    (repo_path / "tests").mkdir()
    (repo_path / "tests" / "test_p.py").write_text("def test_ok():\n    assert True\n")
    await _git(repo_path, "add", "-A")
    await _git(repo_path, "commit", "-m", "initial")

    project = await repo.create_project(db, "crash", str(repo_path))
    run = await repo.create_run(db, project.id, "intent", "run/crash1", 5.0)
    await seed_run_status(db, run.id, RunStatus.ACTIVE.value)
    await _git(repo_path, "branch", run.branch)
    fresh = await repo.get_run(db, run.id)
    assert fresh is not None

    wave = await repo.get_or_create_wave0(db, run.id)
    # task A: already completed before the "crash"
    task_a = await repo.create_task(db, wave.id, 1, "done already", TaskType.CODE_CHANGE)
    await db.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (task_a.id,))
    # task B: caught mid-flight
    task_b = await repo.create_task(
        db, wave.id, 2, "in flight", TaskType.CODE_CHANGE,
        scope_globs=["src/**"], spec_slice_md="## Task\n\nin flight\n",
    )
    await db.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_b.id,))
    await db.conn.commit()

    tip = await _git(repo_path, "rev-parse", run.branch)
    attempt = await repo.create_attempt(db, task_b.id, tip.strip())
    await transition_attempt(db, attempt.id, AttemptStatus.RUNNING)

    manager = WorktreeManager(repo_path, tmp_path / "wt")
    ref = await manager.create(run.id, task_b.id, tip.strip())
    await repo.create_worktree(db, attempt.id, str(ref.path), ref.branch)
    # junk left in the worktree by the "crashed" process
    (ref.path / "src" / "uncommitted.py").write_text("half done\n")

    settings = Settings(
        project=ProjectConfig(test_directories=["tests"]),
        limits=LimitsConfig(
            task_max_attempts=2, attempt_max_turns=6, attempt_wallclock_s=60, planning_turns=0
        ),
        sandbox=SandboxNetwork(),
    )
    return db, project, fresh, repo_path, task_a, task_b, attempt, ref, settings, tmp_path


async def test_crash_recovery_then_clean_resume(seeded) -> None:
    (db, _project, run, repo_path, task_a, task_b, attempt, ref, settings, _tmp) = seeded

    report = await RecoveryService(db, settings).recover()
    assert attempt.id in report.crashed_attempts
    assert task_b.id in report.rescheduled_tasks
    assert str(ref.path) in report.pruned_worktrees

    crashed = await repo.get_attempt(db, attempt.id)
    assert crashed is not None and crashed.status is AttemptStatus.CRASHED
    task_b_fresh = await repo.get_task(db, task_b.id)
    assert task_b_fresh is not None
    assert task_b_fresh.status is TaskStatus.RETRY_SCHEDULED
    wt_row = (await repo.list_worktrees(db))[0]
    assert wt_row.state is WorktreeState.PRUNED
    assert not ref.path.exists()

    # --- the next pump executes exactly one fresh, clean attempt on task B
    sandbox = ScriptSandbox(
        suite_results=[ExecResult(0, "", "")], suite_xml=GREEN_XML
    )
    gateway = FakeGateway(responses=write_commit_complete())
    engine = RunEngine(
        db=db,
        settings=settings,
        secrets=Secrets(),
        gateway=gateway,  # type: ignore[arg-type]
        sandbox=sandbox,
        notifier=None,
        redactor=Redactor(),
        repo_path=repo_path,
    )
    tip_pre = (await _git(repo_path, "rev-parse", run.branch)).strip()
    # Sprint 5 wave semantics: this run has two tasks, so the resume goes
    # through the wave path (single actionable task executes, then serialized
    # integration fast-forwards the run branch). Drive to local green and
    # assert the recovery END-STATE: the invariants below are the contract.
    descriptor = await engine.run_to_completion(run.id)
    assert descriptor == "local_green"

    # completed task A was NOT re-executed: it has zero attempts
    rows_a = await db.fetchall("SELECT * FROM attempts WHERE task_id = ?", (task_a.id,))
    assert rows_a == []
    # exactly one NEW attempt on task B, from the clean run-branch tip
    rows_b = await db.fetchall(
        "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_num", (task_b.id,)
    )
    assert [r["status"] for r in rows_b] == [AttemptStatus.CRASHED.value,
                                             AttemptStatus.SUCCEEDED.value]
    tip = (await _git(repo_path, "rev-parse", run.branch)).strip()
    assert rows_b[1]["base_commit"] == tip_pre  # fresh attempt from the clean base
    assert tip != tip_pre  # and the run branch advanced via the merge
    task_b_done = await repo.get_task(db, task_b.id)
    assert task_b_done is not None and task_b_done.status is TaskStatus.COMPLETED
    fresh_run = await repo.get_run(db, run.id)
    assert fresh_run is not None and fresh_run.status is RunStatus.ACTIVE
