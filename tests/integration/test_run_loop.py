"""Integration: the RunEngine local-green loop on a real temp git repo.

Real git throughout; the sandbox is ScriptSandbox (LocalExecSandbox + scripted
junit suites) — no podman. FakeGateway scripts the single task's attempt.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from girder.config import LimitsConfig, ProjectConfig, SandboxNetwork, Secrets, Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run, RunStatus, TaskStatus
from girder.gitops.branch import BranchOps
from girder.guard.redact import Redactor
from girder.orchestrator.run_engine import RunEngine
from girder.sandbox.engine import ExecResult
from girder.sandbox.local import LocalExecSandbox
from girder.specs.freeze import approve_and_freeze
from girder.util import run_host_cmd
from tests.conftest import seed_run_status
from tests.unit.test_task_engine import (
    FakeGateway,
    ScriptSandbox,
    write_commit_complete,
)

pytestmark = pytest.mark.integration

PROPOSAL = """\
---
schema: girder.openspec/v1
title: Loop test
intent: Drive the full local loop.
tasks:
  - id: do-thing
    title: Do the thing
    type: code_change
    scope_globs: ["src/**"]
    success_criteria: ["done"]
    depends_on: []
---
Narrative.
"""

GREEN_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<testsuite name="pytest" tests="1">'
    '<testcase classname="tests.test_p" name="test_ok" file="tests/test_p.py"/>'
    "</testsuite>"
)
RED_XML = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<testsuite name="pytest" tests="1">'
    '<testcase classname="tests.test_p" name="test_bad" file="tests/test_p.py">'
    "<failure>boom</failure></testcase>"
    "</testsuite>"
)


@dataclass
class FakeNotifier:
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    async def notify(
        self, level: str, title: str, body: str, *, run_id: str | None = None
    ) -> None:
        self.calls.append((level, title, body))


@dataclass
class Ctx:
    db: Database
    project: Project
    run: Run
    repo_path: Path
    notifier: FakeNotifier
    settings: Settings

    def engine(self, gateway: Any, sandbox: LocalExecSandbox) -> RunEngine:
        return RunEngine(
            db=self.db,
            settings=self.settings,
            secrets=Secrets(),
            gateway=gateway,
            sandbox=sandbox,
            notifier=self.notifier,  # type: ignore[arg-type]
            redactor=Redactor(),
            repo_path=self.repo_path,
        )


async def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = await run_host_cmd(["git", "-C", str(cwd), *args], check=check, timeout_s=30)
    return result.stdout


@pytest.fixture
async def ctx(db: Database, tmp_path: Path) -> AsyncIterator[Ctx]:
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

    project = await repo.create_project(db, "loop", str(repo_path))
    run = await repo.create_run(db, project.id, "intent", "run/loop1", 5.0)
    await seed_run_status(db, run.id, RunStatus.SPEC_PENDING.value)
    fresh = await repo.get_run(db, run.id)
    assert fresh is not None
    await approve_and_freeze(
        db, project=project, run=fresh, proposal_text=PROPOSAL,
        repo_path=repo_path, worktree_base=tmp_path / "freeze-wt",
    )
    fresh = await repo.get_run(db, run.id)
    assert fresh is not None and fresh.status is RunStatus.SPEC_APPROVED
    settings = Settings(
        project=ProjectConfig(test_directories=["tests"]),
        limits=LimitsConfig(
            task_max_attempts=2, attempt_max_turns=6, attempt_wallclock_s=60, planning_turns=0
        ),
        sandbox=SandboxNetwork(),
    )
    yield Ctx(
        db=db, project=project, run=fresh, repo_path=repo_path,
        notifier=FakeNotifier(), settings=settings,
    )


async def test_local_green_end_to_end(ctx: Ctx, tmp_path: Path) -> None:
    h = ctx
    sandbox = ScriptSandbox(
        suite_results=[
            ExecResult(0, "", ""),   # baseline suite
            ExecResult(0, "", ""),   # verify suite
        ],
        suite_xml=GREEN_XML,
    )
    gateway = FakeGateway(responses=write_commit_complete())
    engine = h.engine(gateway, sandbox)
    tip_before = await BranchOps(h.repo_path).run_branch_tip(h.run.branch)

    descriptor = await engine.run_to_completion(h.run.id)
    assert descriptor == "local_green"

    fresh = await repo.get_run(h.db, h.run.id)
    assert fresh is not None
    assert fresh.status is RunStatus.ACTIVE  # Sprint 3 terminal: stays active
    assert fresh.baseline_run_id  # baseline was recorded

    tasks = await repo.list_tasks_for_run(h.db, h.run.id)
    assert [t.status for t in tasks] == [TaskStatus.COMPLETED]

    tip_after = await BranchOps(h.repo_path).run_branch_tip(h.run.branch)
    assert tip_after != tip_before  # the task's commit was merged

    events = await h.db.fetchall(
        "SELECT event_type FROM agent_events WHERE run_id = ?", (h.run.id,)
    )
    types = {e["event_type"] for e in events}
    assert {"baseline_started", "baseline_completed", "spec_decomposed",
            "task_completed", "run_local_green"} <= types

    local_green = await repo.get_latest_event(h.db, h.run.id, "run_local_green")
    assert local_green is not None
    assert local_green["payload"]["task_titles"] == ["Do the thing"]

    # idempotent: re-pumping a locally green run does not re-notify
    calls_before = len(h.notifier.calls)
    assert await engine.pump_once(h.run.id) == "local_green"
    assert len(h.notifier.calls) == calls_before


async def test_broken_baseline_escalates(ctx: Ctx) -> None:
    h = ctx
    sandbox = ScriptSandbox(
        suite_results=[
            ExecResult(1, "", "FAILED tests/test_p.py::test_bad"),  # baseline suite
            ExecResult(1, "", "FAILED tests/test_p.py::test_bad"),  # rerun: still broken
        ],
        suite_xml=RED_XML,
    )
    gateway = FakeGateway(responses=[])
    engine = h.engine(gateway, sandbox)
    descriptor = await engine.pump_once(h.run.id)
    assert descriptor == "escalated"

    fresh = await repo.get_run(h.db, h.run.id)
    assert fresh is not None and fresh.status is RunStatus.ESCALATED
    # no tasks were decomposed and no model calls were made
    assert await repo.list_tasks_for_run(h.db, h.run.id) == []
    assert gateway.calls == []
    assert h.notifier.calls
