"""Issue: redundant second suite run after a successful conflict resolution.

After a conflict resolution that already passed the FULL suite at exactly the
landed commit, ``WaveIntegrator._integrate_task`` must skip its per-merge
suite re-run — fail-closed: only an exact sha match skips; an absent verified
sha means the suite runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from girder.config import Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Task, TaskStatus, TaskType, Wave
from girder.fsm import transition_task
from girder.gitops.audit import AuditResult
from girder.guard.redact import Redactor
from girder.orchestrator.conflict import ResolutionOutcome
from girder.orchestrator.integrator import WaveIntegrator
from girder.orchestrator.suites import SuiteResult
from girder.util import run_host_cmd


async def _git(cwd: Path, *args: str) -> str:
    proc = await run_host_cmd(["git", "-C", str(cwd), *args], timeout_s=30)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


async def _commit(cwd: Path, msg: str) -> str:
    await _git(cwd, "add", "-A")
    await _git(cwd, "commit", "-q", "-m", msg)
    return await _git(cwd, "rev-parse", "HEAD")


@pytest.fixture
async def conflict_ctx(db: Database, tmp_path: Path) -> dict[str, Any]:
    """Real git repo + run/wave/task seeded so that merging the task branch
    into the wave branch conflicts (both edited the same docstring line)."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    await _git(repo_path, "init", "-b", "main")
    await _git(repo_path, "config", "user.email", "t@t")
    await _git(repo_path, "config", "user.name", "t")
    (repo_path / "app.py").write_text('"""Doc."""\n\nx = 1\n')
    base = await _commit(repo_path, "init")

    project = await repo.create_project(db, "p", str(repo_path))
    run = await repo.create_run(db, project.id, "intent", "run/x1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    task = await repo.create_task(db, wave.id, 1, "t", TaskType.CODE_CHANGE)

    # wave branch diverges on the shared line...
    await _git(repo_path, "checkout", "-q", "-b", "wave/x1/w0", base)
    (repo_path / "app.py").write_text('"""Wave side."""\n\nx = 1\n')
    wave_tip = await _commit(repo_path, "wave work")
    # ...and so does the task branch (same lines) -> textual conflict.
    await _git(repo_path, "checkout", "-q", "-B", "tmp-task", base)
    (repo_path / "app.py").write_text('"""Task side."""\n\nx = 1\n')
    task_tip = await _commit(repo_path, "task work")
    await _git(repo_path, "branch", "-f", f"task/{task.id}", task_tip)
    await _git(repo_path, "branch", "-f", "run/x1", base)
    await _git(repo_path, "checkout", "-q", "main")

    for status in (
        TaskStatus.SCHEDULED,
        TaskStatus.RUNNING,
        TaskStatus.VERIFYING,
        TaskStatus.VERIFY_PASSED,
    ):
        await transition_task(db, task.id, status)

    integrator = WaveIntegrator(
        db=db,
        gateway=None,  # type: ignore[arg-type]
        sandbox=None,  # type: ignore[arg-type]
        settings=Settings(),
        redactor=Redactor(secret_env_names=[]),
        notifier=None,
        project=project,
        repo_path=repo_path,
        worktree_base=tmp_path / "wt",
    )
    suite_calls: list[str] = []

    async def fake_suite(wave: Wave, task: Task, commit: str) -> SuiteResult:
        suite_calls.append(commit)
        return SuiteResult(green=True, tail="", test_count=1, exit_code=0)

    async def fake_audit(task: Task, tip: str) -> AuditResult:
        return AuditResult(passed=True)

    integrator._suite_at_commit = fake_suite  # type: ignore[method-assign]
    integrator._audit_integrate = fake_audit  # type: ignore[method-assign]
    return {
        "db": db,
        "repo_path": repo_path,
        "integrator": integrator,
        "project": project,
        "run": run,
        "wave": wave,
        "task": task,
        "base": base,
        "task_tip": task_tip,
        "wave_tip": wave_tip,
        "suite_calls": suite_calls,
    }


class StubResolver:
    def __init__(self, outcome: ResolutionOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    async def resolve(self, **_kwargs: Any) -> ResolutionOutcome:
        self.calls += 1
        return self.outcome


async def test_resolved_conflict_at_same_commit_skips_suite(conflict_ctx) -> None:
    """The resolution already passed the full suite at the landed commit —
    the per-merge suite re-run is skipped (resolution commit == new tip)."""
    ctx = conflict_ctx
    # The "resolution commit" is a child of the wave tip that reconciles both
    # sides: craft it as a real merge-free commit on top of the wave tip.
    repo_path: Path = ctx["repo_path"]
    await _git(repo_path, "checkout", "-q", "-B", "resolve", ctx["wave_tip"])
    (repo_path / "app.py").write_text('"""Both sides."""\n\nx = 1\n')
    resolution_commit = await _commit(repo_path, "resolve conflict")
    await _git(repo_path, "checkout", "-q", "main")

    resolver = StubResolver(ResolutionOutcome("resolved", resolution_commit, "ok"))
    outcome = await ctx["integrator"]._integrate_task(
        ctx["run"],
        ctx["wave"],
        "wave/x1/w0",
        ctx["task"],
        resolver,  # type: ignore[arg-type]
        ctx["wave_tip"],
        ["app.py"],
    )
    assert outcome.kind == "task_integrated"
    assert resolver.calls == 1
    assert ctx["suite_calls"] == [], "suite must NOT re-run at the resolution commit"


async def test_resolved_conflict_without_verified_sha_runs_suite(conflict_ctx) -> None:
    """Fail-closed: no verified commit in the outcome -> the suite runs."""
    ctx = conflict_ctx
    resolver = StubResolver(ResolutionOutcome("resolved", None, "ok"))
    outcome = await ctx["integrator"]._integrate_task(
        ctx["run"],
        ctx["wave"],
        "wave/x1/w0",
        ctx["task"],
        resolver,  # type: ignore[arg-type]
        ctx["wave_tip"],
        ["app.py"],
    )
    assert outcome.kind == "task_integrated"
    assert ctx["suite_calls"] == [ctx["wave_tip"]]


async def test_plain_merge_path_runs_suite_once(conflict_ctx) -> None:
    """The plain (no-conflict) merge path is unchanged: exactly one suite run
    at the merged tip and no resolver involvement."""
    ctx = conflict_ctx
    repo_path: Path = ctx["repo_path"]
    base: str = ctx["base"]
    task: Task = ctx["task"]
    # Task branch strictly ahead of the wave branch: fast-forward, no conflict.
    await _git(repo_path, "checkout", "-q", "-B", "tmp2", base)
    (repo_path / "other.py").write_text("y = 2\n")
    task_tip = await _commit(repo_path, "task work ff")
    await _git(repo_path, "branch", "-f", f"task/{task.id}", task_tip)
    await _git(repo_path, "checkout", "-q", "main")
    await _git(repo_path, "branch", "-f", "wave/x1/w0", base)

    resolver = StubResolver(ResolutionOutcome("unresolved", None, "never called"))
    outcome = await ctx["integrator"]._integrate_task(
        ctx["run"],
        ctx["wave"],
        "wave/x1/w0",
        task,
        resolver,  # type: ignore[arg-type]
        base,
        ["app.py"],
    )
    assert outcome.kind == "task_integrated"
    assert resolver.calls == 0
    assert ctx["suite_calls"] == [task_tip]
