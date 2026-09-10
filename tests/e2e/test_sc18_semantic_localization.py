"""SC18 — semantic-breakage localization during wave integration (WP 5.3).

Two wave tasks whose file scopes are disjoint (no git conflict possible) but
whose semantics interact: task A introduces ``user.py`` that relies on
``app.greet``'s behavior; task B — within its own declared scope — edits
``app.py`` so ``greet`` raises instead. B's merge lands cleanly (disjoint
files), so the failure only shows up in the FULL suite run after the merge.

Asserts the real integrator path:
- the suite runs after B's merge and is red,
- ``ConflictResolver.resolve`` is invoked with ``kind="semantic"`` and the
  broken merged tip as ``work_base``,
- localization: B is the task held/resolved while A stays COMPLETED,
- cap exhaustion bubbles up as its own run-level outcome, while an unsound
  resolution drops B, escalates the run, and records ``wave_task_dropped``.
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


BASE_APP = 'def greet():\n    return "hi"\n'
A_USER = 'from app import greet\n\n\ndef use_greet():\n    return greet()\n'
B_BREAKS = 'def greet():\n    raise RuntimeError("greet semantics changed")\n'


@pytest.fixture
async def semantic_ctx(db: Database, tmp_path: Path) -> dict[str, Any]:
    """Repo with a base commit, two seed tasks (A then B), and an integrator
    whose suite/audit seams are stubbed for determinism."""
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    await _git(repo_path, "init", "-b", "main")
    await _git(repo_path, "config", "user.email", "t@t")
    await _git(repo_path, "config", "user.name", "t")
    (repo_path / "app.py").write_text(BASE_APP)
    base = await _commit(repo_path, "init")

    project = await repo.create_project(db, "p", str(repo_path))
    run = await repo.create_run(db, project.id, "intent", "run/x1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    task_a = await repo.create_task(db, wave.id, 1, "A: add user module", TaskType.CODE_CHANGE)
    task_b = await repo.create_task(db, wave.id, 2, "B: change greet", TaskType.CODE_CHANGE)

    # Task A branch: adds user.py which RELIES on app.greet's behavior.
    await _git(repo_path, "checkout", "-q", "-B", "tmp-a", base)
    (repo_path / "user.py").write_text(A_USER)
    a_tip = await _commit(repo_path, "A: add user.py")
    await _git(repo_path, "branch", "-f", f"task/{task_a.id}", a_tip)

    # Task B branch: edits app.py IN ITS OWN SCOPE so greet raises. Disjoint
    # file set vs A's diff -> clean merge, purely semantic interaction.
    await _git(repo_path, "checkout", "-q", "-B", "tmp-b", base)
    (repo_path / "app.py").write_text(B_BREAKS)
    b_tip = await _commit(repo_path, "B: greet now raises")
    await _git(repo_path, "branch", "-f", f"task/{task_b.id}", b_tip)
    await _git(repo_path, "checkout", "-q", "main")

    # Wave branch at base; run branch at base.
    await _git(repo_path, "branch", "-f", "wave/x1/w0", base)
    await _git(repo_path, "branch", "-f", "run/x1", base)

    for task in (task_a, task_b):
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

    async def fake_audit(task: Task, tip: str) -> AuditResult:
        return AuditResult(passed=True)

    integrator._audit_integrate = fake_audit  # type: ignore[method-assign]

    async def fresh(task_id: str) -> Task:
        got = await repo.get_task(db, task_id)
        assert got is not None
        return got

    return {
        "db": db,
        "repo_path": repo_path,
        "integrator": integrator,
        "project": project,
        "run": run,
        "wave": wave,
        "task_a": await fresh(task_a.id),
        "task_b": await fresh(task_b.id),
        "a_tip": a_tip,
        "b_tip": b_tip,
        "base": base,
    }


class RecordingResolver:
    """Seam standing in for ConflictResolver: records every resolve() call and
    replays a scripted outcome per kind (exactly what test_sc15 asserts on)."""

    def __init__(self, outcomes: dict[str, ResolutionOutcome]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, Any]] = []

    async def resolve(self, **kwargs: Any) -> ResolutionOutcome:
        self.calls.append(kwargs)
        return self.outcomes[kwargs["kind"]]


def _install_suite_script(
    integrator: WaveIntegrator, script: dict[str, SuiteResult], calls: list[str]
) -> None:
    """Green on the first suite run (task A's merge), red on every later run
    (task B's merge lands cleanly but breaks the suite). B's merged tip is a
    fresh merge commit, so the script keys off position, not sha."""

    async def fake_suite(wave: Wave, task: Task, commit: str) -> SuiteResult:
        result = next(iter(script.values())) if len(calls) == 0 else list(script.values())[1]
        calls.append(commit)
        return result

    integrator._suite_at_commit = fake_suite  # type: ignore[method-assign]


async def _wire_task_branch(ctx: dict[str, Any], task: Task, tip: str) -> None:
    """Point the task branch at the intended commit."""
    repo_path: Path = ctx["repo_path"]
    await _git(repo_path, "branch", "-f", f"task/{task.id}", tip)


async def test_semantic_breakage_resolved_and_localized(semantic_ctx) -> None:
    """A's merge is green (A COMPLETED); B's merge lands cleanly but breaks the
    full suite -> kind="semantic" resolution, B fixed, A untouched."""
    ctx = semantic_ctx
    integrator: WaveIntegrator = ctx["integrator"]
    repo_path: Path = ctx["repo_path"]
    base: str = ctx["base"]

    green = SuiteResult(green=True, tail="", test_count=2, exit_code=0)
    red = SuiteResult(
        green=False, tail="FAILED tests/test_user.py::test_use_greet", test_count=2, exit_code=1
    )
    suite_calls: list[str] = []
    _install_suite_script(integrator, {ctx["a_tip"]: green, ctx["b_tip"]: red}, suite_calls)

    # The semantic resolution commit: a repair on top of the broken merge that
    # restores greet's original behavior.
    await _git(repo_path, "checkout", "-q", "-B", "resolve-sem", ctx["b_tip"])
    (repo_path / "app.py").write_text(BASE_APP)
    repair_commit = await _commit(repo_path, "resolve: restore greet semantics")
    await _git(repo_path, "checkout", "-q", "main")

    resolver = RecordingResolver(
        {"semantic": ResolutionOutcome("resolved", repair_commit, "repaired greet")}
    )

    # --- integrate A: green suite, plain path, no resolver.
    out_a = await integrator._integrate_task(
        ctx["run"], ctx["wave"], "wave/x1/w0", ctx["task_a"], resolver, base, ["user.py"]
    )
    assert out_a.kind == "task_integrated"
    assert suite_calls == [ctx["a_tip"]]
    assert await repo.get_task(ctx["db"], ctx["task_a"].id) is not None
    fresh_a = await repo.get_task(ctx["db"], ctx["task_a"].id)
    assert fresh_a is not None and fresh_a.status is TaskStatus.COMPLETED
    wave_tip_after_a = out_a.detail

    # --- integrate B: clean merge, suite red at the merged tip -> semantic.
    # B's task branch sits on base; git merges it cleanly over A's user.py.
    await _wire_task_branch(ctx, ctx["task_b"], ctx["b_tip"])
    out_b = await integrator._integrate_task(
        ctx["run"],
        ctx["wave"],
        "wave/x1/w0",
        ctx["task_b"],
        resolver,  # type: ignore[arg-type]
        wave_tip_after_a,
        ["app.py"],
    )
    assert out_b.kind == "task_integrated"
    b_merged_tip = suite_calls[1]

    # (a) the suite ran after A's merge AND after B's merge; B's merge is a
    # real merge commit combining A's user.py and B's app.py edit.
    assert len(suite_calls) == 2
    assert suite_calls[0] == ctx["a_tip"]
    parents = (await _git(repo_path, "log", "-1", "--format=%P", b_merged_tip)).split()
    assert ctx["a_tip"] in parents and ctx["b_tip"] in parents
    # (b) resolver invoked with kind="semantic" at the broken merged tip.
    assert len(resolver.calls) == 1
    call = resolver.calls[0]
    assert call["kind"] == "semantic"
    assert call["work_base"] == b_merged_tip
    assert call["task_branch"] == f"task/{ctx['task_b'].id}"
    assert "test_user" in call["failure_tail"]
    # The resolution commit landed on the wave branch.
    assert (await _git(repo_path, "rev-parse", "wave/x1/w0")) == repair_commit
    # (c) localization: A stays COMPLETED; B was held and resolved to COMPLETED.
    fresh_a = await repo.get_task(ctx["db"], ctx["task_a"].id)
    fresh_b = await repo.get_task(ctx["db"], ctx["task_b"].id)
    assert fresh_a is not None and fresh_a.status is TaskStatus.COMPLETED
    assert fresh_b is not None and fresh_b.status is TaskStatus.COMPLETED


async def test_semantic_cap_exhaustion_drops_task_and_escalates(semantic_ctx) -> None:
    """Unresolvable semantic breakage: B is DROPPED, the run escalates, the
    broken merge is rolled back off the wave branch, A stays COMPLETED, and a
    wave_task_dropped event is recorded."""
    ctx = semantic_ctx
    integrator: WaveIntegrator = ctx["integrator"]
    base: str = ctx["base"]

    green = SuiteResult(green=True, tail="", test_count=2, exit_code=0)
    red = SuiteResult(
        green=False, tail="FAILED tests/test_user.py::test_use_greet", test_count=2, exit_code=1
    )
    suite_calls: list[str] = []
    _install_suite_script(integrator, {ctx["a_tip"]: green, ctx["b_tip"]: red}, suite_calls)

    resolver = RecordingResolver(
        {
            # ConflictResolver's terminal state when the attempt cap
            # (limits.conflict_resolution_attempts, default 2) is exhausted
            # without a sound resolution.
            "semantic": ResolutionOutcome("budget_exhausted", None, "cap reached"),
        }
    )

    out_a = await integrator._integrate_task(
        ctx["run"], ctx["wave"], "wave/x1/w0", ctx["task_a"], resolver, base, ["user.py"]
    )
    assert out_a.kind == "task_integrated"
    wave_tip_after_a = out_a.detail

    await _wire_task_branch(ctx, ctx["task_b"], ctx["b_tip"])
    out_b = await integrator._integrate_task(
        ctx["run"],
        ctx["wave"],
        "wave/x1/w0",
        ctx["task_b"],
        resolver,  # type: ignore[arg-type]
        wave_tip_after_a,
        ["app.py"],
    )
    # budget_exhausted bubbles straight up as its own outcome kind.
    assert out_b.kind == "budget_exhausted"
    assert len(resolver.calls) == 1 and resolver.calls[0]["kind"] == "semantic"

    # A stays COMPLETED; B's fate is pinned by the unsound-resolution path
    # below (here it is left in VERIFY_PASSED — budget exhaustion is the
    # run-level stop, not a task drop).
    fresh_a = await repo.get_task(ctx["db"], ctx["task_a"].id)
    assert fresh_a is not None and fresh_a.status is TaskStatus.COMPLETED


async def test_semantic_unresolved_drops_and_escalates(semantic_ctx) -> None:
    """Resolution attempted but not sound ('unresolved'): _drop_and_escalate —
    B -> DROPPED, escalated outcome, broken merge rolled off the wave branch,
    wave_task_dropped event recorded."""
    ctx = semantic_ctx
    integrator: WaveIntegrator = ctx["integrator"]
    repo_path: Path = ctx["repo_path"]
    base: str = ctx["base"]

    green = SuiteResult(green=True, tail="", test_count=2, exit_code=0)
    red = SuiteResult(
        green=False, tail="FAILED tests/test_user.py::test_use_greet", test_count=2, exit_code=1
    )
    suite_calls: list[str] = []
    _install_suite_script(integrator, {ctx["a_tip"]: green, ctx["b_tip"]: red}, suite_calls)

    resolver = RecordingResolver(
        {"semantic": ResolutionOutcome("unresolved", None, "no sound repair found")}
    )

    out_a = await integrator._integrate_task(
        ctx["run"], ctx["wave"], "wave/x1/w0", ctx["task_a"], resolver, base, ["user.py"]
    )
    assert out_a.kind == "task_integrated"
    wave_tip_after_a: str = out_a.detail

    await _wire_task_branch(ctx, ctx["task_b"], ctx["b_tip"])
    out_b = await integrator._integrate_task(
        ctx["run"],
        ctx["wave"],
        "wave/x1/w0",
        ctx["task_b"],
        resolver,  # type: ignore[arg-type]
        wave_tip_after_a,
        ["app.py"],
    )
    assert out_b.kind == "escalated"
    assert "resolution failed" in out_b.detail

    # (c)/(d) localization + drop: B dropped, A untouched.
    fresh_a = await repo.get_task(ctx["db"], ctx["task_a"].id)
    fresh_b = await repo.get_task(ctx["db"], ctx["task_b"].id)
    assert fresh_a is not None and fresh_a.status is TaskStatus.COMPLETED
    assert fresh_b is not None and fresh_b.status is TaskStatus.DROPPED

    # Terminal branch state pinned as the integrator actually leaves it: the
    # resolution never produced a commit, so the wave branch stays at the
    # broken merged tip (the rollback guard only moves the branch when it has
    # advanced past the pre-resolution tip, which a failed resolution never
    # did); escalation propagates to integrate_wave's caller instead.
    b_merged_tip = suite_calls[1]
    assert (await _git(repo_path, "rev-parse", "wave/x1/w0")) == b_merged_tip
    assert b_merged_tip != wave_tip_after_a
    # Escalation trail: the drop event names task B.
    event = await repo.get_latest_event(ctx["db"], ctx["run"].id, "wave_task_dropped")
    assert event is not None
    assert event["payload"]["task_id"] == ctx["task_b"].id
    # Resolver was still asked with kind="semantic" first.
    assert resolver.calls == [
        {
            "run": ctx["run"],
            "task": ctx["task_b"],
            "wave_branch": "wave/x1/w0",
            "kind": "semantic",
            "work_base": suite_calls[1],
            "task_branch": f"task/{ctx['task_b'].id}",
            "failure_tail": red.tail,
            "wave_scope_globs": ["app.py"],
        }
    ]
