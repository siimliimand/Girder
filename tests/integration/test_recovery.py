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


# ------------------------------------------------ delivery-state branch repair


async def _delivery_run(
    db: Database, tmp_path: Path, status: str = RunStatus.PR_OPEN.value
) -> dict[str, object]:
    """A repo whose run branch exists only on a bare origin, with the run
    parked in a delivery state (branch deleted locally to simulate the loss)."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    await _git(repo_dir, "init", "-b", "main")
    await _git(repo_dir, "config", "user.email", "t@t")
    await _git(repo_dir, "config", "user.name", "t")
    (repo_dir / "app.py").write_text("v1\n")
    await _git(repo_dir, "add", "-A")
    await _git(repo_dir, "commit", "-m", "init")
    await _git(repo_dir, "branch", "run/del1")

    bare = tmp_path / "origin.git"
    await _git(tmp_path, "init", "--bare", "-b", "main", str(bare))
    await _git(repo_dir, "remote", "add", "origin", str(bare))
    await _git(repo_dir, "push", "-q", "origin", "main", "run/del1")
    # The PR head commit is NOT local main: it is ahead of it.
    await _git(repo_dir, "checkout", "-q", "run/del1")
    (repo_dir / "feature.py").write_text("f = 1\n")
    await _git(repo_dir, "add", "-A")
    await _git(repo_dir, "commit", "-m", "pr work")
    remote_tip = await _git_out(repo_dir, "rev-parse", "run/del1")
    await _git(repo_dir, "push", "-q", "origin", "run/del1")
    await _git(repo_dir, "checkout", "-q", "main")
    await _git(repo_dir, "branch", "-D", "run/del1")

    project = await repo.create_project(db, "del-test", str(repo_dir))
    run = await repo.create_run(db, project.id, "intent", "run/del1", 5.0)
    await seed_run_status(db, run.id, status)
    return {"db": db, "repo_dir": repo_dir, "run_id": run.id, "remote_tip": remote_tip}


async def test_delivery_branch_restored_from_remote(db: Database, tmp_path: Path) -> None:
    """A delivery-state run with a missing local branch is restored from the
    remote head (NOT recreated at main), and the action is audited."""
    ctx = await _delivery_run(db, tmp_path)
    repo_dir: Path = ctx["repo_dir"]  # type: ignore[assignment]

    service = RecoveryService(db, Settings())
    report = await service.recover()

    assert report.restored_branches == ["run/del1"]
    assert report.recreated_branches == []
    tip = await _git_out(repo_dir, "rev-parse", "run/del1")
    assert tip == ctx["remote_tip"], "branch must be at the remote (PR) head"
    event = await repo.get_latest_event(db, str(ctx["run_id"]), "run_branch_restored_from_remote")
    assert event is not None


async def test_delivery_branch_without_remote_left_for_operator(
    db: Database, tmp_path: Path
) -> None:
    """No reachable remote: the branch is NOT rebuilt at main; an audit event
    is written, the recovery report + notification flag it for a human."""
    ctx = await _delivery_run(db, tmp_path)
    repo_dir: Path = ctx["repo_dir"]  # type: ignore[assignment]
    await _git(repo_dir, "remote", "remove", "origin")

    sent: list[tuple[str, str, str]] = []

    class FakeNotifier:
        async def notify(self, level: str, title: str, body: str, **_: object) -> None:
            sent.append((level, title, body))

    service = RecoveryService(db, Settings(), notifier=FakeNotifier())  # type: ignore[arg-type]
    report = await service.recover()

    assert report.attention_branches == ["run/del1"]
    assert report.restored_branches == []
    assert report.recreated_branches == []
    probe = await run_host_cmd(
        ["git", "-C", str(repo_dir), "rev-parse", "--verify", "-q", "run/del1"],
        check=False,
        timeout_s=30,
    )
    assert probe.returncode != 0  # never silently rebuilt at main
    event = await repo.get_latest_event(db, str(ctx["run_id"]), "run_branch_unrestorable")
    assert event is not None
    assert report.anything_recovered
    assert sent, "recovery report notification must go out"
    assert "operator attention" in sent[0][2]


async def test_empty_repository_run_left_for_operator(db: Database, tmp_path: Path) -> None:
    """A run on a repository with zero commits: no base ref resolves, so the
    missing run branch cannot be recreated. Recovery must leave it for
    operator attention and keep boot alive (regression: 'git branch <b> HEAD'
    used to raise 'not a valid object name' and crash the whole daemon)."""
    repo_dir = tmp_path / "empty-repo"
    repo_dir.mkdir()
    await _git(repo_dir, "init", "-b", "master")  # no commit, like a fresh checkout

    project = await repo.create_project(db, "empty-repo", str(repo_dir))
    run = await repo.create_run(db, project.id, "intent", "run/empty1", 5.0)
    await seed_run_status(db, run.id, RunStatus.SPEC_PENDING.value)

    service = RecoveryService(db, Settings())
    report = await service.recover()  # must not raise

    assert report.unresolved_bases == [f"run/empty1 ({repo_dir})"]
    assert report.recreated_branches == []
    assert report.restored_branches == []
    probe = await run_host_cmd(
        ["git", "-C", str(repo_dir), "rev-parse", "--verify", "-q", "run/empty1"],
        check=False,
        timeout_s=30,
    )
    assert probe.returncode != 0  # still nothing to point the branch at
    assert report.anything_recovered


async def test_worktree_manager_list_stale_is_the_65_contract(crashed_state) -> None:
    """impl-plan §6.5 ``WorktreeManager.list_stale()`` delegates to the repo's
    find_stale_worktrees query: active rows whose attempt/task went terminal."""
    db: Database = crashed_state["db"]
    manager = WorktreeManager(crashed_state["repo_dir"], base=crashed_state["base"])

    # Attempt still RUNNING: nothing stale yet.
    assert await manager.list_stale(db) == []

    await transition_attempt(
        db, crashed_state["attempt"].id, AttemptStatus.CRASHED, payload={"reason": "test"}
    )
    stale = await manager.list_stale(db)
    assert [w.path for w in stale] == [str(crashed_state["worktree"])]
    assert stale[0].attempt_id == crashed_state["attempt"].id


async def test_recovery_resets_drifted_run_branch_to_expected_commit(
    crashed_state,
) -> None:
    """impl-plan §6.12: recovery must verify the run branch exists at the
    *expected* commit (the last integrated tip in the DB), not merely that it
    exists — a drifted branch is force-reset and the repair is audited."""
    db: Database = crashed_state["db"]
    repo_dir: Path = crashed_state["repo_dir"]
    run = crashed_state["run"]

    # The DB's last integrated tip: a commit the run branch pointed at.
    await _git(repo_dir, "checkout", "-q", "run/rec1")
    (repo_dir / "integrated.py").write_text("wave = 1\n")
    await _git(repo_dir, "add", "-A")
    await _git(repo_dir, "commit", "-m", "wave work")
    expected = await _git_out(repo_dir, "rev-parse", "run/rec1")
    await repo.insert_event(
        db, "wave_integrated", {"wave_id": "w0", "sequence_order": 0, "tip": expected},
        run_id=run.id,
    )
    await _git(repo_dir, "checkout", "-q", "main")
    # Drift: something force-moved the run branch back to main.
    await _git(repo_dir, "update-ref", "refs/heads/run/rec1", "main")
    assert await _git_out(repo_dir, "rev-parse", "run/rec1") != expected

    service = RecoveryService(db, Settings())
    report = await service.recover()

    assert report.corrected_branches == ["run/rec1"]
    assert await _git_out(repo_dir, "rev-parse", "run/rec1") == expected
    event = await repo.get_latest_event(db, run.id, "run_branch_tip_corrected")
    assert event is not None
    payload = event["payload"]
    assert payload["was"] != expected and payload["reset_to"] == expected


async def test_recovery_leaves_branch_alone_without_expected_tip(crashed_state) -> None:
    """No wave_integrated event ⇒ no expected commit derivable: the branch
    identity check must stay a no-op (§6.12 fallback to existence check)."""
    db: Database = crashed_state["db"]
    repo_dir: Path = crashed_state["repo_dir"]
    before = await _git_out(repo_dir, "rev-parse", "run/rec1")

    service = RecoveryService(db, Settings())
    report = await service.recover()

    assert report.corrected_branches == []
    assert await _git_out(repo_dir, "rev-parse", "run/rec1") == before
