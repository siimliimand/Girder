"""Unit tests for the Diagnostic Fix Agent (D5, impl-plan §6.11).

Mirrors tests/unit/test_task_engine.py: real temp git repo, ScriptSandbox over
LocalExecSandbox with scripted junit results, FakeGateway — no podman.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from girder.config import LimitsConfig, ProjectConfig, SandboxNetwork, Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run, RunStatus, TaskType
from girder.github.diagnostic import DiagnosticEngine, DiagnosticOutcome, count_fix_tasks
from girder.guard.redact import Redactor
from girder.sandbox.engine import ExecResult
from girder.util import run_host_cmd
from tests.conftest import seed_run_status
from tests.unit.test_task_engine import GREEN_XML, FakeGateway, ScriptSandbox, _resp, _tc

pytestmark = pytest.mark.integration

TOKEN = "ghp_abcdef1234567890abcdef1234567890ABC"
FAILURE_REPORT = (
    "FAILED tests/test_calc.py::test_divide - assert ZeroDivisionError\n"
    "logs: token=" + TOKEN + "\n"
)


@dataclass
class FakeNotifier:
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    async def notify(
        self, level: str, title: str, body: str, *, run_id: str | None = None
    ) -> None:
        self.calls.append((level, title, body))


def fix_turns() -> list[Any]:
    """A compliant fix attempt: edit ordinary code in scope, commit, done."""
    return [
        _resp(
            calls=[
                _tc("1", "write_file", '{"path":"src/lib.py","content":"x = 1  # fixed\\n"}')
            ]
        ),
        _resp(calls=[_tc("2", "run_command", '{"cmd":"git add -A && git commit -m fix"}')]),
        _resp(calls=[_tc("3", "mark_task_complete", '{"summary":"fixed CI"}')]),
    ]


def sneaky_test_turns() -> list[Any]:
    """An integrity-violating fix: edits a test file to make the suite pass."""
    return [
        _resp(
            calls=[
                _tc(
                    "1",
                    "write_file",
                    '{"path":"tests/test_p.py",'
                    '"content":"def test_ok():\\n    assert True\\n\\n\\n'
                    'def test_sneaky():\\n    assert 1 == 2\\n"}',
                )
            ]
        ),
        _resp(calls=[_tc("2", "run_command", '{"cmd":"git add -A && git commit -m sneaky"}')]),
        _resp(calls=[_tc("3", "mark_task_complete", '{"summary":"done"}')]),
    ]


@dataclass
class Harness:
    db: Database
    project: Project
    run: Run
    repo_path: Path
    wt_base: Path
    sandbox: ScriptSandbox
    notifier: FakeNotifier
    settings: Settings

    def engine(
        self, gateway: FakeGateway, sandbox: ScriptSandbox | None = None
    ) -> DiagnosticEngine:
        return DiagnosticEngine(
            db=self.db,
            gateway=gateway,  # type: ignore[arg-type]
            sandbox=sandbox or self.sandbox,
            settings=self.settings,
            redactor=Redactor(),
            notifier=self.notifier,  # type: ignore[arg-type]
            project=self.project,
            repo_path=self.repo_path,
            worktree_base=self.wt_base,
        )


@pytest.fixture
async def harness(db: Database, tmp_path: Path) -> AsyncIterator[Harness]:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    for args in (("init", "-b", "main"), ("config", "user.email", "test@girder.local"),
                 ("config", "user.name", "girder-test")):
        await run_host_cmd(["git", "-C", str(repo_path), *args], timeout_s=30)
    (repo_path / "src").mkdir()
    (repo_path / "src" / "lib.py").write_text("x = 1\n")
    (repo_path / "tests").mkdir()
    (repo_path / "tests" / "test_p.py").write_text("def test_ok():\n    assert True\n")
    await run_host_cmd(["git", "-C", str(repo_path), "add", "-A"], timeout_s=30)
    await run_host_cmd(["git", "-C", str(repo_path), "commit", "-m", "initial"], timeout_s=30)

    project = await repo.create_project(db, "p", str(repo_path))
    run = await repo.create_run(db, project.id, "intent", "run/diag1", 5.0)
    await seed_run_status(db, run.id, RunStatus.CI_FIXING.value)
    run = await repo.get_run(db, run.id)
    assert run is not None
    await run_host_cmd(["git", "-C", str(repo_path), "branch", run.branch], timeout_s=30)

    settings = Settings(
        project=ProjectConfig(test_directories=["tests"]),
        limits=LimitsConfig(
            task_max_attempts=2,
            attempt_max_turns=6,
            attempt_wallclock_s=60,
            ci_fix_attempts=1,
        ),
        sandbox=SandboxNetwork(),
    )
    yield Harness(
        db=db,
        project=project,
        run=run,
        repo_path=repo_path,
        wt_base=tmp_path / "wt",
        sandbox=ScriptSandbox(suite_results=[ExecResult(0, "", "")], suite_xml=GREEN_XML),
        notifier=FakeNotifier(),
        settings=settings,
    )


async def test_cap_and_success_cycle(harness: Harness) -> None:
    h = harness
    gateway = FakeGateway(responses=fix_turns())
    outcome = await h.engine(gateway).run(run=h.run, failure_report=FAILURE_REPORT)
    assert outcome.kind == "fixed" and outcome.detail is not None
    assert await count_fix_tasks(h.db, h.run.id) == 1

    tasks = await repo.list_tasks_for_run(h.db, h.run.id)
    assert len(tasks) == 1
    assert tasks[0].task_type is TaskType.FIX
    assert tasks[0].scope_globs == ["**"]

    # the fix actually merged onto the run branch (tip moved past main)
    tip = await run_host_cmd(
        ["git", "-C", str(h.repo_path), "rev-parse", h.run.branch], timeout_s=30
    )
    head = await run_host_cmd(
        ["git", "-C", str(h.repo_path), "rev-parse", "main"], timeout_s=30
    )
    assert tip.stdout != head.stdout

    # cap (ci_fix_attempts=1) is now exhausted: no NEW task may be created
    outcome2 = await h.engine(gateway).run(run=h.run, failure_report=FAILURE_REPORT)
    assert outcome2 == DiagnosticOutcome("failed", "ci fix attempts exhausted")
    assert await count_fix_tasks(h.db, h.run.id) == 1  # no new task
    assert not gateway.responses  # first run consumed its script; second ran no model


async def test_ci_fix_attempted_event_and_failure_report_redaction(harness: Harness) -> None:
    h = harness
    gateway = FakeGateway(responses=fix_turns())
    outcome = await h.engine(gateway).run(run=h.run, failure_report=FAILURE_REPORT)
    assert outcome.kind == "fixed"

    events = await h.db.fetchall(
        "SELECT event_type, payload_json FROM agent_events"
        " WHERE event_type = 'ci_fix_attempted' AND run_id = ?",
        (h.run.id,),
    )
    assert len(events) == 1
    assert json.loads(events[0]["payload_json"])["outcome"] == "completed"

    task = (await repo.list_tasks_for_run(h.db, h.run.id))[0]
    assert "ZeroDivisionError" in task.spec_slice_md
    assert TOKEN not in task.spec_slice_md
    assert "ghp***[REDACTED:" in task.spec_slice_md


async def test_test_file_tampering_fix_fails_integrity(harness: Harness) -> None:
    h = harness
    gateway = FakeGateway(responses=sneaky_test_turns())
    outcome = await h.engine(gateway).run(run=h.run, failure_report=FAILURE_REPORT)
    # Layer 1-3 still protect tests for fix tasks: the fix cannot land.
    assert outcome.kind == "failed"

    task = (await repo.list_tasks_for_run(h.db, h.run.id))[0]
    fresh = await repo.get_task(h.db, task.id)
    assert fresh is not None and fresh.status.value == "failed"
    viols = await h.db.fetchall("SELECT kind FROM integrity_violations")
    assert {"test_path_modified", "content_hash_mismatch"} <= {v["kind"] for v in viols}
    # run branch never moved
    tip = await run_host_cmd(
        ["git", "-C", str(h.repo_path), "rev-parse", h.run.branch], timeout_s=30
    )
    head = await run_host_cmd(
        ["git", "-C", str(h.repo_path), "rev-parse", "main"], timeout_s=30
    )
    assert tip.stdout == head.stdout
