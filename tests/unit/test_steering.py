"""Unit tests for mid-flight steering (Sprint 6 WP 6.2).

Pause/resume/abort/skip/force_pass on the RunEngine pump, mid-turn inject
absorption in the agent runtime, and inject folding in the task engine.
Real temp git repo + ScriptSandbox (no podman) — same idioms as
tests/unit/test_task_engine.py.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from girder.agent import prompts
from girder.agent.runtime import AgentRuntime
from girder.config import LimitsConfig, ProjectConfig, SandboxNetwork, Secrets, Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import RunStatus, SteeringKind, TaskStatus, TaskType
from girder.fsm import transition_task
from girder.guard.redact import Redactor
from girder.guard.scope import TaskScopes
from girder.orchestrator.run_engine import RunEngine
from girder.sandbox.engine import ExecResult
from tests.conftest import seed_run_status
from tests.unit.test_agent_runtime import FakeGateway, _resp, _tc
from tests.unit.test_agent_tools import TASK, FakeSandbox
from tests.unit.test_task_engine import GREEN_XML, ScriptSandbox, write_commit_complete

pytestmark = pytest.mark.integration


class Harness:
    def __init__(
        self,
        db: Database,
        run_id: str,
        repo_path: Path,
        wt_base: Path,
        settings: Settings,
        sandbox: ScriptSandbox,
    ) -> None:
        self.db = db
        self.run_id = run_id
        self.repo_path = repo_path
        self.wt_base = wt_base
        self.settings = settings
        self.sandbox = sandbox

    def engine(self, gateway: FakeGateway) -> RunEngine:
        return RunEngine(
            db=self.db,
            settings=self.settings,
            secrets=Secrets(models_openrouter_api_key="sk-test-nonsecret"),
            gateway=gateway,  # type: ignore[arg-type]
            sandbox=self.sandbox,
            notifier=None,
            redactor=Redactor(),
            repo_path=self.repo_path,
        )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        project=ProjectConfig(test_directories=["tests"]),
        limits=LimitsConfig(task_max_attempts=2, attempt_max_turns=6, attempt_wallclock_s=60),
        sandbox=SandboxNetwork(),
    )


@pytest.fixture
async def harness(db: Database, settings: Settings, tmp_path: Path) -> AsyncIterator[Harness]:
    """An active run on a real temp git repo, run branch at HEAD."""
    from girder.util import run_host_cmd

    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    for args in (
        ["git", "-C", str(repo_path), "init", "-b", "main"],
        ["git", "-C", str(repo_path), "config", "user.email", "test@girder.local"],
        ["git", "-C", str(repo_path), "config", "user.name", "girder-test"],
    ):
        await run_host_cmd(args, timeout_s=30)
    (repo_path / "src").mkdir()
    (repo_path / "src" / "lib.py").write_text("x = 1\n")
    (repo_path / "tests").mkdir()
    (repo_path / "tests" / "test_p.py").write_text("def test_ok():\n    assert True\n")
    await run_host_cmd(["git", "-C", str(repo_path), "add", "-A"], timeout_s=30)
    await run_host_cmd(["git", "-C", str(repo_path), "commit", "-m", "initial"], timeout_s=30)

    project = await repo.create_project(db, "p", str(repo_path))
    run = await repo.create_run(db, project.id, "intent", "run/steer1", 5.0)
    await seed_run_status(db, run.id, RunStatus.ACTIVE.value)
    await run_host_cmd(["git", "-C", str(repo_path), "branch", "run/steer1"], timeout_s=30)

    sandbox = ScriptSandbox(suite_results=[ExecResult(0, "", "")], suite_xml=GREEN_XML)
    yield Harness(
        db=db,
        run_id=run.id,
        repo_path=repo_path,
        wt_base=tmp_path / "wt",
        settings=settings,
        sandbox=sandbox,
    )


async def _add_task(h: Harness, run_id: str, *, seq: int, scope_globs: list[str]) -> Any:
    wave = await repo.get_or_create_wave0(h.db, run_id)
    return await repo.create_task(
        h.db,
        wave.id,
        seq,
        f"task {seq}",
        TaskType.CODE_CHANGE,
        scope_globs=scope_globs,
        spec_slice_md="## Task\n\nDo it.\n",
    )


async def _events(db: Database, run_id: str, event_type: str) -> list[dict[str, Any]]:
    rows = await db.fetchall(
        "SELECT payload_json FROM agent_events WHERE run_id = ? AND event_type = ? ORDER BY id",
        (run_id, event_type),
    )
    return [{"payload": json.loads(r["payload_json"])} for r in rows]


async def test_pause_parks_run_without_executing(harness: Harness) -> None:
    h = harness
    await _add_task(h, h.run_id, seq=1, scope_globs=["src/**"])
    gateway = FakeGateway(responses=write_commit_complete())
    await repo.insert_steering_event(h.db, h.run_id, SteeringKind.PAUSE.value, {})

    descriptor = await h.engine(gateway).pump_once(h.run_id)
    assert descriptor == "paused"
    fresh = await repo.get_run(h.db, h.run_id)
    assert fresh is not None and fresh.paused
    assert gateway.calls == []  # zero model calls while paused


async def test_paused_run_holds_until_resume(harness: Harness) -> None:
    h = harness
    await _add_task(h, h.run_id, seq=1, scope_globs=["src/**"])
    gateway = FakeGateway(responses=write_commit_complete())
    await repo.insert_steering_event(h.db, h.run_id, SteeringKind.PAUSE.value, {})
    engine = h.engine(gateway)
    assert await engine.pump_once(h.run_id) == "paused"

    # a second pump with no resume event keeps holding
    assert await engine.pump_once(h.run_id) == "paused"
    assert gateway.calls == []

    # resume: the same pump falls through and executes the task
    await repo.insert_steering_event(h.db, h.run_id, SteeringKind.RESUME.value, {})
    assert await engine.pump_once(h.run_id) == "task_completed"
    fresh = await repo.get_run(h.db, h.run_id)
    assert fresh is not None and not fresh.paused
    assert len(gateway.calls) == 3
    task = (await h.db.fetchall("SELECT id, status FROM tasks"))[0]
    assert task["status"] == TaskStatus.COMPLETED.value
    resume_events = await _events(h.db, h.run_id, "steering_resume")
    assert resume_events


async def test_abort_wins_while_paused_and_clears_flag(harness: Harness) -> None:
    h = harness
    await _add_task(h, h.run_id, seq=1, scope_globs=["src/**"])
    gateway = FakeGateway(responses=write_commit_complete())
    engine = h.engine(gateway)
    await repo.insert_steering_event(h.db, h.run_id, SteeringKind.PAUSE.value, {})
    assert await engine.pump_once(h.run_id) == "paused"

    await repo.insert_steering_event(h.db, h.run_id, SteeringKind.ABORT.value, {})
    assert await engine.pump_once(h.run_id) == "aborted"
    fresh = await repo.get_run(h.db, h.run_id)
    assert fresh is not None and fresh.status is RunStatus.ABORTED
    assert not fresh.paused


async def test_skip_steering_skips_task_and_runs_the_rest(harness: Harness) -> None:
    h = harness
    t1 = await _add_task(h, h.run_id, seq=1, scope_globs=["src/**"])
    await _add_task(h, h.run_id, seq=2, scope_globs=["docs/**"])
    gateway = FakeGateway(responses=write_commit_complete())
    await repo.insert_steering_event(h.db, h.run_id, SteeringKind.SKIP.value, {"task_id": t1.id})

    descriptor = await h.engine(gateway).pump_once(h.run_id)
    assert descriptor == "wave_executed"
    statuses = {r["id"]: r["status"] for r in await h.db.fetchall("SELECT id, status FROM tasks")}
    assert statuses[t1.id] == TaskStatus.SKIPPED.value
    assert all(s != TaskStatus.SKIPPED.value for k, s in statuses.items() if k != t1.id)
    applied = await _events(h.db, h.run_id, "steering_applied")
    assert applied


async def test_skip_on_terminal_or_foreign_task_is_ignored(harness: Harness) -> None:
    h = harness
    # drive T1 to completed via allowed edges so it is terminal
    t1 = await _add_task(h, h.run_id, seq=1, scope_globs=["src/**"])
    for state in (
        TaskStatus.SCHEDULED,
        TaskStatus.RUNNING,
        TaskStatus.VERIFYING,
        TaskStatus.VERIFY_PASSED,
        TaskStatus.COMPLETED,
    ):
        await transition_task(h.db, t1.id, state)
    # a task belonging to ANOTHER run
    other_project = await repo.create_project(h.db, "other", str(h.repo_path))
    other_run = await repo.create_run(h.db, other_project.id, "i", "run/steer2", 5.0)
    await seed_run_status(h.db, other_run.id, RunStatus.ACTIVE.value)
    foreign = await _add_task(h, other_run.id, seq=1, scope_globs=["src/**"])

    await repo.insert_steering_event(h.db, h.run_id, SteeringKind.SKIP.value, {"task_id": t1.id})
    await repo.insert_steering_event(
        h.db, h.run_id, SteeringKind.FORCE_PASS.value, {"task_id": foreign.id}
    )
    gateway = FakeGateway(responses=[])
    assert await h.engine(gateway).pump_once(h.run_id) == "local_green"

    ignored = await _events(h.db, h.run_id, "steering_ignored")
    reasons = " ".join(str(r["payload"]) for r in ignored)
    assert "already terminal" in reasons
    assert "another run" in reasons
    assert not await _events(h.db, h.run_id, "steering_applied")


async def test_inject_absorbed_mid_turn_as_trusted_message(db: Database) -> None:
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc9", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    task_row = await repo.create_task(db, wave.id, 1, "t", TaskType.CODE_CHANGE)
    attempt = await repo.create_attempt(db, task_row.id, "abc123")
    await repo.insert_steering_event(
        db,
        run.id,
        SteeringKind.INJECT.value,
        {"directive": "please focus on error handling"},
    )
    gateway = FakeGateway([_resp(calls=[_tc("1", "mark_task_complete", '{"summary":"ok"}')])])
    runtime = AgentRuntime(
        gateway=gateway,  # type: ignore[arg-type]
        sandbox=FakeSandbox(),
        container="ctr",
        scopes=TaskScopes(write_globs=["src/**"]),
        limits=LimitsConfig(attempt_max_turns=3),
        redactor=Redactor(),
        db=db,
        run_id=run.id,
        attempt=attempt,
        task=TASK,
    )
    outcome = await runtime.execute_attempt(spec_slice="s")
    assert outcome.status == "succeeded"

    expected = prompts.build_directive_message("please focus on error handling")
    assert expected.startswith("[TRUSTED] Steering directive (user-authored):")
    assert any(m.role == "user" and m.content == expected for m in gateway.calls[0][1])
    event = await repo.get_latest_event(db, run.id, "steering_injected")
    assert event is not None
    assert event["payload"]["directive"] == "please focus on error handling"


async def test_task_engine_folds_unconsumed_inject_into_guidance(harness: Harness) -> None:
    h = harness
    task = await _add_task(h, h.run_id, seq=1, scope_globs=["src/**"])
    await repo.insert_steering_event(
        h.db,
        h.run_id,
        SteeringKind.INJECT.value,
        {"directive": "keep the public API unchanged"},
    )
    gateway = FakeGateway(responses=write_commit_complete())
    from girder.orchestrator.task_engine import TaskEngine

    run_row = await repo.get_run(h.db, h.run_id)
    assert run_row is not None
    project = await repo.get_project(h.db, run_row.project_id)
    assert project is not None
    task_engine = TaskEngine(
        db=h.db,
        gateway=gateway,  # type: ignore[arg-type]
        sandbox=h.sandbox,
        settings=h.settings,
        redactor=Redactor(),
        notifier=None,
        project=project,
        repo_path=h.repo_path,
        worktree_base=h.wt_base,
    )
    outcome = await task_engine.execute_task(run_row, task)  # type: ignore[arg-type]
    assert outcome.kind == "completed"
    first_user = next(m for m in gateway.calls[0][1] if m.role == "user")
    assert "keep the public API unchanged" in first_user.content
    assert "[TRUSTED] Steering directive (user-authored):" in first_user.content
    # consumed by the engine, so the runtime never saw it
    assert await repo.get_latest_event(h.db, h.run_id, "steering_injected") is None


def test_build_directive_message_tags_and_strips() -> None:
    out = prompts.build_directive_message("  use small diffs  \n")
    assert out == "[TRUSTED] Steering directive (user-authored):\nuse small diffs"
