"""Unit tests for the sequential scheduler (WP 3.5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run, Task, TaskStatus, TaskType
from girder.orchestrator.scheduler import (
    TERMINAL_WAVE_STATUSES,
    Scheduler,
    WavePlanError,
    compute_levels,
    expand_globs,
    files_overlap,
    plan_waves,
)
from girder.util import run_host_cmd


async def _seed(db: Database, n_tasks: int) -> tuple[Project, Run, list[Task]]:
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/sched1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    tasks = [
        await repo.create_task(db, wave.id, seq, f"t{seq}", TaskType.CODE_CHANGE)
        for seq in range(1, n_tasks + 1)
    ]
    return project, run, tasks


async def _set_status(db: Database, task: Task, status: str) -> None:
    await db.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task.id))
    await db.conn.commit()


async def test_ordering_follows_wave_then_seq(db: Database) -> None:
    _project, run, tasks = await _seed(db, 3)
    scheduler = Scheduler(db)
    picked = await scheduler.next_task(run)
    assert picked is not None and picked.id == tasks[0].id
    await _set_status(db, tasks[0], TaskStatus.RUNNING.value)
    picked = await scheduler.next_task(run)
    assert picked is not None and picked.id == tasks[1].id


async def test_retry_scheduled_is_pickable_running_is_not(db: Database) -> None:
    _project, run, tasks = await _seed(db, 2)
    scheduler = Scheduler(db)
    await _set_status(db, tasks[0], TaskStatus.RETRY_SCHEDULED.value)
    picked = await scheduler.next_task(run)
    assert picked is not None and picked.id == tasks[0].id

    await _set_status(db, tasks[0], TaskStatus.AWAITING_AMENDMENT.value)
    await _set_status(db, tasks[1], TaskStatus.RUNNING.value)
    assert await scheduler.next_task(run) is None


async def test_pending_or_retry_count(db: Database) -> None:
    _project, run, tasks = await _seed(db, 3)
    scheduler = Scheduler(db)
    assert await scheduler.pending_or_retry_count(run) == 3
    await _set_status(db, tasks[0], TaskStatus.RUNNING.value)
    await _set_status(db, tasks[1], TaskStatus.RETRY_SCHEDULED.value)
    await _set_status(db, tasks[2], TaskStatus.COMPLETED.value)
    assert await scheduler.pending_or_retry_count(run) == 1


async def test_all_terminal_and_all_completed(db: Database) -> None:
    _project, run, tasks = await _seed(db, 2)
    scheduler = Scheduler(db)
    assert not await scheduler.all_terminal(run)
    assert not await scheduler.all_completed(run)

    await _set_status(db, tasks[0], TaskStatus.COMPLETED.value)
    await _set_status(db, tasks[1], TaskStatus.DROPPED.value)
    assert await scheduler.all_terminal(run)
    assert not await scheduler.all_completed(run)

    await _set_status(db, tasks[1], TaskStatus.COMPLETED.value)
    assert await scheduler.all_completed(run)


async def test_all_completed_counts_operator_skipped_tasks(db: Database) -> None:
    """Sprint 6: skip / force-pass steering (Phase 5) is operator-sanctioned
    success — a run must reach local green with such tasks, not fail."""
    _project, run, tasks = await _seed(db, 3)
    scheduler = Scheduler(db)
    await _set_status(db, tasks[0], TaskStatus.COMPLETED.value)
    await _set_status(db, tasks[1], TaskStatus.SKIPPED.value)
    await _set_status(db, tasks[2], TaskStatus.FORCE_PASSED.value)
    assert await scheduler.all_completed(run)
    assert await scheduler.all_terminal(run)


async def test_empty_run_is_not_terminal(db: Database) -> None:
    _project, run, _tasks = await _seed(db, 0)
    scheduler = Scheduler(db)
    assert not await scheduler.all_terminal(run)
    assert not await scheduler.all_completed(run)
    assert await scheduler.next_task(run) is None


# ------------------------------------------------------------- wave planner (WP 5.1)


def _task(
    tid: str, seq: int, *, globs: list[str] | None = None, deps: list[str] | None = None
) -> Task:
    return Task(
        id=tid,
        wave_id="w0",
        seq=seq,
        title=tid,
        task_type=TaskType.CODE_CHANGE,
        status=TaskStatus.PENDING,
        scope_globs=globs or [],
        depends_on=deps or [],
    )


TREE = ["src/a.py", "src/a/one.py", "src/b.py", "src/b/two.py", "tests/x.py", "README.md"]


def _seqs(waves: list[list[Task]]) -> list[list[str]]:
    return [[t.id for t in wave] for wave in waves]


def test_compute_levels_no_deps_all_zero() -> None:
    tasks = [_task("a", 1), _task("b", 2), _task("c", 3)]
    assert compute_levels(tasks) == {"a": 0, "b": 0, "c": 0}


def test_compute_levels_chain() -> None:
    tasks = [_task("a", 1), _task("b", 2, deps=["a"]), _task("c", 3, deps=["b"])]
    assert compute_levels(tasks) == {"a": 0, "b": 1, "c": 2}


def test_compute_levels_diamond() -> None:
    tasks = [
        _task("a", 1),
        _task("b", 2, deps=["a"]),
        _task("c", 3, deps=["a"]),
        _task("d", 4, deps=["b", "c"]),
    ]
    assert compute_levels(tasks) == {"a": 0, "b": 1, "c": 1, "d": 2}


def test_compute_levels_unknown_dep_raises() -> None:
    with pytest.raises(WavePlanError):
        compute_levels([_task("a", 1, deps=["ghost"])])


def test_compute_levels_cycle_raises() -> None:
    tasks = [_task("a", 1, deps=["b"]), _task("b", 2, deps=["a"])]
    with pytest.raises(WavePlanError):
        compute_levels(tasks)


def test_expand_globs_recursive_and_exact() -> None:
    assert expand_globs(["src/**"], TREE) == {
        "src/a.py",
        "src/a/one.py",
        "src/b.py",
        "src/b/two.py",
    }
    assert expand_globs(["src/a.py"], TREE) == {"src/a.py"}


def test_files_overlap() -> None:
    assert files_overlap({"src/a.py"}, {"src/a.py", "tests/x.py"})
    assert not files_overlap({"src/a.py"}, {"tests/x.py"})


def test_plan_waves_mechanical_demotion_of_overlapping_pair() -> None:
    """WP DoD: no depends_on, identical globs -> later seq is demoted despite
    the model claiming independence."""
    tasks = [_task("a", 1, globs=["src/**"]), _task("b", 2, globs=["src/**"])]
    waves = plan_waves(tasks, TREE)
    assert _seqs(waves) == [["a"], ["b"]]


def test_plan_waves_disjoint_globs_share_wave() -> None:
    tasks = [
        _task("a", 1, globs=["src/a/**"]),
        _task("b", 2, globs=["src/b/**"]),
    ]
    assert _seqs(plan_waves(tasks, TREE)) == [["a", "b"]]


def test_plan_waves_overlap_decided_against_tree_not_globs() -> None:
    # Globs differ, but the tree has no src/b* file, so b's expansion is empty
    # and no overlap exists against the tree.
    tree = ["src/a.py", "src/a/one.py"]
    tasks = [_task("a", 1, globs=["src/a*"]), _task("b", 2, globs=["src/b*"])]
    assert _seqs(plan_waves(tasks, tree)) == [["a", "b"]]


async def test_plan_waves_demotion_is_transitive_through_depends_on(
    db: Database, tmp_path: Path
) -> None:
    tasks = [
        _task("a", 1, globs=["src/**"]),
        _task("b", 2, globs=["src/**"]),
        _task("c", 3, globs=["other/**"], deps=["b"]),
    ]
    waves = plan_waves(tasks, TREE)
    ids = {t.id: i for i, wave in enumerate(waves) for t in wave}
    assert ids["a"] == 0
    assert ids["b"] == 1
    assert ids["c"] >= 2

    # Audit reporting: the transitively demoted task (c moves only because its
    # dependency b was demoted by glob overlap) must appear in the
    # waves_planned event's demoted_task_ids too.
    repo_path = await _seed_git_repo(tmp_path)
    project = await repo.create_project(db, "p", str(repo_path))
    run = await repo.create_run(db, project.id, "intent", "main", 5.0)
    wave0 = await repo.get_or_create_wave0(db, run.id)
    ta = await repo.create_task(db, wave0.id, 1, "a", TaskType.CODE_CHANGE, scope_globs=["src/**"])
    tb = await repo.create_task(db, wave0.id, 2, "b", TaskType.CODE_CHANGE, scope_globs=["src/**"])
    tc = await repo.create_task(
        db, wave0.id, 3, "c", TaskType.CODE_CHANGE, scope_globs=["other/**"], depends_on=[tb.id]
    )
    await Scheduler(db).plan_run_waves(run, repo_path)
    event = await repo.get_latest_event(db, run.id, "waves_planned")
    assert event is not None
    demoted = event["payload"]["demoted_task_ids"]
    assert ta.id not in demoted
    assert tb.id in demoted
    assert tc.id in demoted  # transitively demoted through b
    assert event["payload"]["wave_assignment"] == {
        ta.id: 0,
        tb.id: 1,
        tc.id: 2,
    }


def test_plan_waves_three_pairwise_overlapping_tasks_stagger() -> None:
    tasks = [
        _task("a", 1, globs=["src/**"]),
        _task("b", 2, globs=["src/**"]),
        _task("c", 3, globs=["src/**"]),
    ]
    assert _seqs(plan_waves(tasks, TREE)) == [["a"], ["b"], ["c"]]


def test_plan_waves_preserves_seq_order_and_covers_all_tasks_once() -> None:
    tasks = [
        _task("a", 2, globs=["src/a/**"]),
        _task("b", 1, globs=["src/a/**"]),
        _task("c", 3, globs=["src/b/**"]),
        _task("d", 4, globs=["tests/**"]),
    ]
    waves = plan_waves(tasks, TREE)
    flat = [t.id for wave in waves for t in wave]
    assert sorted(flat) == ["a", "b", "c", "d"]
    assert len(flat) == len(set(flat))
    for wave in waves:
        assert [t.seq for t in wave] == sorted(t.seq for t in wave)
    # b (seq 1) and a (seq 2) overlap -> a demoted behind b.
    ids = {t.id: i for i, wave in enumerate(waves) for t in wave}
    assert ids["b"] < ids["a"]


def test_plan_waves_empty() -> None:
    assert plan_waves([], TREE) == []


# ------------------------------------------------------- wave materialization


async def _seed_git_repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "src").mkdir()
    (repo_path / "src" / "a.py").write_text("a = 1\n")
    (repo_path / "tests").mkdir()
    (repo_path / "tests" / "x.py").write_text("def test_x():\n    pass\n")
    for argv in (
        ["git", "init", "-b", "main", str(repo_path)],
        ["git", "-C", str(repo_path), "config", "user.email", "t@t"],
        ["git", "-C", str(repo_path), "config", "user.name", "t"],
        ["git", "-C", str(repo_path), "add", "."],
        ["git", "-C", str(repo_path), "commit", "-m", "init"],
    ):
        proc = await run_host_cmd(argv)
        assert proc.returncode == 0, proc.stderr
    return repo_path


async def test_plan_run_waves_materializes_and_is_idempotent(db: Database, tmp_path: Path) -> None:
    repo_path = await _seed_git_repo(tmp_path)
    project = await repo.create_project(db, "p", str(repo_path))
    run = await repo.create_run(db, project.id, "intent", "main", 5.0)
    wave0 = await repo.get_or_create_wave0(db, run.id)
    ta = await repo.create_task(
        db, wave0.id, 1, "a", TaskType.CODE_CHANGE, scope_globs=["src/**"]
    )
    tb = await repo.create_task(
        db, wave0.id, 2, "b", TaskType.CODE_CHANGE, scope_globs=["src/**"]
    )
    scheduler = Scheduler(db)

    waves = await scheduler.plan_run_waves(run, repo_path)
    assert [w.sequence_order for w in waves] == [0, 1]
    wave0_tasks = await repo.list_tasks_for_wave(db, waves[0].id)
    wave1_tasks = await repo.list_tasks_for_wave(db, waves[1].id)
    assert [t.id for t in wave0_tasks] == [ta.id]
    assert [t.id for t in wave1_tasks] == [tb.id]

    event = await repo.get_latest_event(db, run.id, "waves_planned")
    assert event is not None
    assert event["payload"]["demoted_task_ids"] == [tb.id]
    assert event["payload"]["waves"][1]["task_ids"] == [tb.id]

    # Idempotent: second call returns the same rows, no duplicate events.
    again = await scheduler.plan_run_waves(run, repo_path)
    assert [w.id for w in again] == [w.id for w in waves]
    count = len(
        await db.fetchall(
            "SELECT id FROM agent_events WHERE event_type = 'waves_planned'"
        )
    )
    assert count == 1


async def test_plan_run_waves_does_not_report_dependency_placement_as_demotion(
    db: Database, tmp_path: Path
) -> None:
    """A task at level 1 purely through depends_on edges is NOT mechanically
    demoted; a same-level glob-overlapped task IS (issue: demoted_task_ids
    over-reported). Full placement stays available under ``wave_assignment``."""
    repo_path = await _seed_git_repo(tmp_path)
    project = await repo.create_project(db, "p", str(repo_path))
    run = await repo.create_run(db, project.id, "intent", "main", 5.0)
    wave0 = await repo.get_or_create_wave0(db, run.id)
    ta = await repo.create_task(
        db, wave0.id, 1, "a", TaskType.CODE_CHANGE, scope_globs=["src/a.py"]
    )
    tb = await repo.create_task(
        db,
        wave0.id,
        2,
        "b",
        TaskType.CODE_CHANGE,
        scope_globs=["src/b.py"],
        depends_on=[ta.id],  # disjoint scope — level 1 comes from the edge alone
    )
    tc = await repo.create_task(
        db, wave0.id, 3, "c", TaskType.CODE_CHANGE, scope_globs=["src/**"]
    )
    (repo_path / "src" / "b.py").write_text("b = 1\n")
    proc = await run_host_cmd(["git", "-C", str(repo_path), "add", "."])
    assert proc.returncode == 0
    proc = await run_host_cmd(
        ["git", "-C", str(repo_path), "commit", "-q", "-m", "b"]
    )
    assert proc.returncode == 0

    waves = await Scheduler(db).plan_run_waves(run, repo_path)
    assert [w.sequence_order for w in waves] == [0, 1, 2]
    event = await repo.get_latest_event(db, run.id, "waves_planned")
    assert event is not None
    assert event["payload"]["demoted_task_ids"] == [tc.id]
    assert event["payload"]["wave_assignment"] == {ta.id: 0, tb.id: 1, tc.id: 2}


async def test_next_wave_skips_terminal_and_returns_none_when_all_done(
    db: Database,
) -> None:
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/w", 5.0)
    w0 = await repo.get_or_create_wave0(db, run.id)
    w1 = await repo.create_wave(db, run.id, 1)
    scheduler = Scheduler(db)

    assert await scheduler.next_wave(run) == w0
    await repo.set_wave_status(db, w0.id, "completed")
    assert await scheduler.next_wave(run) == w1
    await repo.set_wave_status(db, w1.id, "failed")
    assert await scheduler.next_wave(run) is None


def test_terminal_wave_statuses() -> None:
    assert TERMINAL_WAVE_STATUSES == frozenset({"completed", "failed"})
