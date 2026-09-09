"""WP-1.9 — recovery: synthetic mid-crash state reconciles on boot.

Phase 0 exit criterion: a state machine run transitions states across a
simulated restart. Here we simulate the crash *after* the DB and git state
have diverged from reality (attempt 'running', worktree on disk), then boot
RecoveryService and verify it converges.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from girder.config import Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import AttemptStatus, RunStatus, TaskStatus, TaskType
from girder.fsm import transition_attempt, transition_task
from girder.gitops.worktree import WorktreeManager
from girder.orchestrator.recovery import RecoveryService
from girder.util import run_host_cmd
from tests.conftest import seed_run_status

pytestmark = pytest.mark.integration


async def _git(cwd: Path, *args: str) -> None:
    await run_host_cmd(["git", "-C", str(cwd), *args], timeout_s=30)


async def _git_out(cwd: Path, *args: str) -> str:
    proc = await run_host_cmd(["git", "-C", str(cwd), *args], timeout_s=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
async def crashed_state(db: Database, tmp_path: Path):  # type: ignore[no-untyped-def]
    """A full project→run→task→attempt chain frozen mid-attempt, plus the
    worktree the crashed attempt was using (still on disk)."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    await _git(repo_dir, "init", "-b", "main")
    await _git(repo_dir, "config", "user.email", "t@t")
    await _git(repo_dir, "config", "user.name", "t")
    (repo_dir / "app.py").write_text("v1\n")
    await _git(repo_dir, "add", "-A")
    await _git(repo_dir, "commit", "-m", "init")
    await _git(repo_dir, "branch", "run/rec1")  # the run branch, like baseline does

    project = await repo.create_project(db, "rec-test", str(repo_dir))
    run = await repo.create_run(db, project.id, "intent", "run/rec1", 5.0)
    await seed_run_status(db, run.id, RunStatus.ACTIVE.value)
    wave = await repo.get_or_create_wave0(db, run.id)
    task = await repo.create_task(
        db, wave.id, 1, "Task", TaskType.CODE_CHANGE, scope_globs=["src/**"]
    )
    await transition_task(db, task.id, TaskStatus.SCHEDULED)
    await transition_task(db, task.id, TaskStatus.RUNNING)
    attempt = await repo.create_attempt(db, task.id, "HEAD")
    await transition_attempt(db, attempt.id, AttemptStatus.RUNNING)

    base = tmp_path / "worktrees"
    manager = WorktreeManager(repo_dir, base=base)
    wt_ref = await manager.create(run.id, task.id, "HEAD")
    await repo.create_worktree(db, attempt.id, str(wt_ref.path), wt_ref.branch)
    return {
        "db": db,
        "repo_dir": repo_dir,
        "run": run,
        "task": task,
        "attempt": attempt,
        "worktree": wt_ref.path,
        "base": base,
    }


async def test_recover_after_simulated_crash(crashed_state) -> None:  # type: ignore[no-untyped-def]
    db: Database = crashed_state["db"]
    task, attempt = crashed_state["task"], crashed_state["attempt"]

    service = RecoveryService(db, Settings())
    report = await service.recover()

    # attempt: running -> crashed, ended, failure reason recorded
    fresh_attempt = await repo.get_attempt(db, attempt.id)
    assert fresh_attempt is not None
    assert fresh_attempt.status == AttemptStatus.CRASHED
    assert fresh_attempt.failure_reason and "restart" in fresh_attempt.failure_reason

    # task: running -> retry_scheduled (attempt budget NOT exhausted: 1 < 3)
    fresh_task = await repo.get_task(db, task.id)
    assert fresh_task is not None
    assert fresh_task.status == TaskStatus.RETRY_SCHEDULED
    assert report.rescheduled_tasks == [task.id]

    # worktree: pruned from disk and marked pruned in DB
    assert not crashed_state["worktree"].exists()
    wts = await repo.list_worktrees(db)
    assert len(wts) == 1 and wts[0].state.value == "pruned"
    assert report.crashed_attempts == [attempt.id]
    assert report.pruned_worktrees == [str(crashed_state["worktree"])]
    assert report.anything_recovered


async def test_recover_clean_state_is_noop(db: Database) -> None:
    service = RecoveryService(db, Settings())
    report = await service.recover()
    assert not report.anything_recovered
    assert "nothing to recover" in report.summary()


async def test_recover_fails_task_when_budget_exhausted(crashed_state) -> None:  # type: ignore[no-untyped-def]
    db: Database = crashed_state["db"]
    task = crashed_state["task"]
    # exhaust the attempt budget (as if prior attempts already burned through)
    await repo.update_task_fields(db, task.id, attempts_used=3)

    service = RecoveryService(db, Settings())
    report = await service.recover()

    fresh_task = await repo.get_task(db, task.id)
    assert fresh_task is not None
    assert fresh_task.status == TaskStatus.FAILED
    assert report.failed_tasks == [task.id]


async def test_recovery_notifies_with_redacted_summary(
    crashed_state,
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    db: Database = crashed_state["db"]

    sent: list[tuple[str, str, str]] = []

    class FakeNotifier:
        async def notify(self, level: str, title: str, body: str, **_: object) -> None:
            sent.append((level, title, body))

    service = RecoveryService(db, Settings(), notifier=FakeNotifier())  # type: ignore[arg-type]
    await service.recover()

    assert len(sent) == 1
    level, title, _ = sent[0]
    assert level == "warning"
    assert "restarted" in title


async def test_recovery_recreates_deleted_run_branch(crashed_state) -> None:
    """§8.7 step 4: a non-terminal run whose branch is missing from git gets
    it recreated from main, and the action lands in the report."""
    db: Database = crashed_state["db"]
    repo_dir: Path = crashed_state["repo_dir"]
    await _git(repo_dir, "branch", "-D", "run/rec1")
    probe = await run_host_cmd(
        ["git", "-C", str(repo_dir), "rev-parse", "--verify", "-q", "run/rec1"],
        check=False,
        timeout_s=30,
    )
    assert probe.returncode != 0

    service = RecoveryService(db, Settings())
    report = await service.recover()

    assert report.recreated_branches == ["run/rec1"]
    main_tip = await _git_out(repo_dir, "rev-parse", "main")
    assert await _git_out(repo_dir, "rev-parse", "run/rec1") == main_tip


async def test_recovery_prunes_worktree_row_with_missing_path(crashed_state) -> None:
    """§8.7 step 4: a worktree row whose path vanished is pruned (the DB row
    is the stale side) so the scheduler rebuilds cleanly from base_commit."""
    import shutil

    db: Database = crashed_state["db"]
    shutil.rmtree(crashed_state["worktree"])
    assert not crashed_state["worktree"].exists()

    service = RecoveryService(db, Settings())
    report = await service.recover()

    assert report.pruned_worktrees == [str(crashed_state["worktree"])]
    wts = await repo.list_worktrees(db)
    assert len(wts) == 1 and wts[0].state.value == "pruned"
    # the task was rescheduled and can be rebuilt from its base commit
    fresh_task = await repo.get_task(db, crashed_state["task"].id)
    assert fresh_task is not None
    assert fresh_task.status == TaskStatus.RETRY_SCHEDULED
