"""WP-1.6 — worktree manager + GC daemon against a real temp git repo."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from girder.db import repo
from girder.db.engine import Database
from girder.db.models import AttemptStatus, RunStatus, TaskStatus, TaskType
from girder.fsm import transition_attempt, transition_task
from girder.gitops.worktree import WorktreeManager
from girder.orchestrator.gc import WorktreeGC
from girder.util import run_host_cmd
from tests.conftest import seed_run_status

pytestmark = pytest.mark.integration


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
    (repo_dir / "app.py").write_text("print('v1')\n")
    (repo_dir / "tests").mkdir()
    (repo_dir / "tests" / "test_app.py").write_text("def test_ok(): assert True\n")
    await _git(repo_dir, "add", "-A")
    await _git(repo_dir, "commit", "-m", "initial")
    return repo_dir


@dataclass
class Seeded:
    db: Database
    base: Path
    run_id: str
    task_id: str
    attempt_id: str
    worktree_path: Path
    worktree_row_id: str


@pytest.fixture
async def seeded(db: Database, git_repo: Path, tmp_path: Path) -> Seeded:
    """project → run(active) → wave → task(running) → attempt(running) + worktree."""
    base = tmp_path / "worktrees"
    project = await repo.create_project(db, "gc-test", str(git_repo))
    run = await repo.create_run(db, project.id, "intent", "run/gc1", 5.0)
    await seed_run_status(db, run.id, RunStatus.ACTIVE.value)
    wave = await repo.get_or_create_wave0(db, run.id)
    task = await repo.create_task(
        db,
        wave.id,
        1,
        "Do thing",
        TaskType.CODE_CHANGE,
        scope_globs=["src/**"],
    )
    await transition_task(db, task.id, TaskStatus.SCHEDULED)
    await transition_task(db, task.id, TaskStatus.RUNNING)
    attempt = await repo.create_attempt(db, task.id, "HEAD")
    await transition_attempt(db, attempt.id, AttemptStatus.RUNNING)

    manager = WorktreeManager(git_repo, base=base)
    wt_ref = await manager.create(run.id, task.id, "HEAD")
    wt_row = await repo.create_worktree(db, attempt.id, str(wt_ref.path), wt_ref.branch)
    return Seeded(
        db=db,
        base=base,
        run_id=run.id,
        task_id=task.id,
        attempt_id=attempt.id,
        worktree_path=wt_ref.path,
        worktree_row_id=wt_row.id,
    )


# ------------------------------------------------------------------ worktrees


async def test_create_worktree_with_branch(git_repo: Path, tmp_path: Path) -> None:
    manager = WorktreeManager(git_repo, base=tmp_path / "wts")
    ref = await manager.create("run1", "task1", "HEAD")

    assert ref.path.is_dir()
    assert (ref.path / "app.py").read_text() == "print('v1')\n"
    assert ref.branch == "task/task1"

    branches = await _git(git_repo, "branch", "--list", "task/task1")
    assert "task/task1" in branches
    assert str(ref.path) in (await manager.list())


async def test_commits_in_worktree_do_not_touch_main(git_repo: Path, tmp_path: Path) -> None:
    manager = WorktreeManager(git_repo, base=tmp_path / "wts")
    ref = await manager.create("run1", "task1", "HEAD")

    (ref.path / "app.py").write_text("print('v2')\n")
    await _git(ref.path, "add", "-A")
    await _git(ref.path, "commit", "-m", "change in task")

    assert (git_repo / "app.py").read_text() == "print('v1')\n"  # main untouched
    assert (ref.path / "app.py").read_text() == "print('v2')\n"


async def test_remove_worktree_cleans_up(git_repo: Path, tmp_path: Path) -> None:
    manager = WorktreeManager(git_repo, base=tmp_path / "wts")
    ref = await manager.create("run1", "task1", "HEAD")
    (ref.path / "app.py").write_text("dirty")
    await manager.remove(ref.path, force=True)  # force handles dirty trees

    assert not ref.path.exists()
    assert str(ref.path) not in (await manager.list())


async def test_create_over_existing_path_rejected(git_repo: Path, tmp_path: Path) -> None:
    manager = WorktreeManager(git_repo, base=tmp_path / "wts")
    ref = await manager.create("run1", "task1", "HEAD")
    with pytest.raises(FileExistsError):
        await manager.create("run1", "task1", "HEAD")
    await manager.remove(ref.path, force=True)


async def test_recreate_after_crash_reuses_stale_branch(git_repo: Path, tmp_path: Path) -> None:
    """D8: a crash leaves the task branch behind; the next attempt must still
    get a worktree over the same base commit."""
    manager = WorktreeManager(git_repo, base=tmp_path / "wts")
    ref = await manager.create("run1", "task1", "HEAD")
    shutil.rmtree(ref.path)  # simulate crash: dir vanishes without git cleanup
    await _git(git_repo, "worktree", "prune")

    ref2 = await manager.create("run1", "task1", "HEAD")
    assert ref2.path.is_dir()
    await manager.remove(ref2.path, force=True)


async def test_heal_orphan_clears_crash_leftover(git_repo: Path, tmp_path: Path) -> None:
    """An attempt crashing mid-_start_attempt leaves the worktree behind;
    heal_orphan clears it so the retry can provision fresh."""
    manager = WorktreeManager(git_repo, base=tmp_path / "wts")
    ref = await manager.create("run1", "task1", "HEAD")

    await manager.heal_orphan("run1", "task1")

    assert not ref.path.exists()
    ref2 = await manager.create("run1", "task1", "HEAD")  # retry provisions
    assert ref2.path.is_dir()
    await manager.remove(ref2.path, force=True)


async def test_heal_orphan_removes_unregistered_leftover_dir(
    git_repo: Path, tmp_path: Path
) -> None:
    """A crash can leave a bare (non-git-registered) directory; heal_orphan
    still clears it — the namespace is WorktreeManager-owned."""
    manager = WorktreeManager(git_repo, base=tmp_path / "wts")
    leftover = tmp_path / "wts" / "run2" / "task2"
    leftover.mkdir(parents=True)
    (leftover / "junk").write_text("x")

    await manager.heal_orphan("run2", "task2")

    assert not leftover.exists()


async def test_heal_orphan_noop_when_path_absent(git_repo: Path, tmp_path: Path) -> None:
    manager = WorktreeManager(git_repo, base=tmp_path / "wts")
    await manager.heal_orphan("runX", "taskX")  # must not raise
    assert not (tmp_path / "wts" / "runX").exists()


# ------------------------------------------------------------------------ GC


async def test_gc_prunes_terminal_task_worktrees(seeded: Seeded) -> None:
    assert seeded.worktree_path.is_dir()
    for state in (TaskStatus.VERIFYING, TaskStatus.VERIFY_PASSED, TaskStatus.COMPLETED):
        await transition_task(seeded.db, seeded.task_id, state)

    gc = WorktreeGC(seeded.db, base=seeded.base, poll_interval_s=0.05)
    result = await asyncio.wait_for(gc.run_once(), timeout=30)

    assert str(seeded.worktree_path) in result.pruned
    assert not seeded.worktree_path.exists()
    fresh = await repo.get_worktree(seeded.db, seeded.worktree_row_id)
    assert fresh is not None and fresh.state.value == "pruned"


async def test_gc_leaves_active_attempts_alone(seeded: Seeded) -> None:
    gc = WorktreeGC(seeded.db, base=seeded.base)
    result = await gc.run_once()
    assert result.pruned == []
    assert seeded.worktree_path.is_dir()


async def test_gc_terminal_attempt_also_prunes(seeded: Seeded) -> None:
    await transition_attempt(seeded.db, seeded.attempt_id, AttemptStatus.CRASHED)
    gc = WorktreeGC(seeded.db, base=seeded.base)
    result = await gc.run_once()
    assert str(seeded.worktree_path) in result.pruned


async def test_gc_disk_guard_edge_triggered(seeded: Seeded) -> None:
    gc = WorktreeGC(seeded.db, base=seeded.base, alert_pct=0.0)  # always "full"
    first = await gc.run_once()
    assert first.disk_alert is not None and "worktree volume" in first.disk_alert
    second = await gc.run_once()  # still above threshold: no re-alert
    assert second.disk_alert is None

    # recovering below the threshold re-arms the trigger
    gc.alert_pct = 101.0
    await gc.run_once()
    gc.alert_pct = 0.0
    third = await gc.run_once()
    assert third.disk_alert is not None


async def test_gc_quarantines_unremovable_worktrees(seeded: Seeded, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    for state in (TaskStatus.VERIFYING, TaskStatus.VERIFY_PASSED, TaskStatus.COMPLETED):
        await transition_task(seeded.db, seeded.task_id, state)

    async def broken_remove(self: object, path: object, *, force: bool = True) -> None:  # type: ignore[no-untyped-def]
        raise RuntimeError("git exploded")

    monkeypatch.setattr(WorktreeManager, "remove", broken_remove)
    gc = WorktreeGC(seeded.db, base=seeded.base)
    result = await gc.run_once()
    assert str(seeded.worktree_path) in result.quarantined
    fresh = await repo.get_worktree(seeded.db, seeded.worktree_row_id)
    assert fresh is not None and fresh.state.value == "quarantined"
