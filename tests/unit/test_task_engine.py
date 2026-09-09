"""Unit tests for the TaskEngine attempt lifecycle (WP 3.5).

Real temp git repo; ScriptSandbox executes tool commands on the host via
LocalExecSandbox but intercepts the junit suite commands (baseline/verify/
rerun) with scripted results — no podman.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from girder.config import LimitsConfig, ProjectConfig, SandboxNetwork, Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import (
    AttemptStatus,
    Project,
    Run,
    RunStatus,
    Task,
    TaskStatus,
    TaskType,
    WorktreeState,
)
from girder.gitops.branch import BranchOps
from girder.guard.redact import Redactor
from girder.orchestrator.task_engine import TaskEngine, TaskOutcome
from girder.sandbox.engine import ContainerSpec, ExecResult
from girder.sandbox.local import LocalExecSandbox
from girder.util import run_host_cmd
from tests.conftest import seed_run_status

pytestmark = pytest.mark.integration

PROPOSAL = """\
---
schema: girder.openspec/v1
title: Engine test
intent: Test the task engine.
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
    "<failure>assert 1 == 2</failure></testcase>"
    "</testsuite>"
)


class ScriptSandbox(LocalExecSandbox):
    """LocalExecSandbox with scripted junit suite results.

    Any exec whose argv mentions a ``--junitxml=/workspace/.girder-*.xml``
    report is intercepted: the scripted ExecResult is returned and the
    matching XML is materialized on the host-visible worktree. Everything
    else (write_file plumbing, git commit, ...) really executes on the host.
    """

    def __init__(self, suite_results: list[ExecResult], suite_xml: str) -> None:
        super().__init__()
        self._suite_results = list(suite_results)
        self._suite_xml = suite_xml
        self.suite_cmds: list[list[str]] = []

    async def exec(
        self, name: str, cmd: list[str], *, timeout_s: float = 120.0, user: str | None = None
    ) -> ExecResult:
        if any(".girder-" in arg and ".xml" in arg for arg in cmd):
            self.suite_cmds.append(cmd)
            result = self._suite_results.pop(0) if self._suite_results else ExecResult(0, "", "")
            worktree = self.worktree_of(name)
            report = next(
                (a.split("=", 1)[1] for a in cmd if a.startswith("--junitxml=")), None
            )
            if report is not None:
                rel = report.removeprefix("/workspace/")
                (worktree / rel).write_text(self._suite_xml)
            return result
        return await super().exec(name, cmd, timeout_s=timeout_s, user=user)


@dataclass
class FakeNotifier:
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    async def notify(
        self, level: str, title: str, body: str, *, run_id: str | None = None
    ) -> None:
        self.calls.append((level, title, body))


@dataclass
class FakeGateway:
    """Scripted per-call responses; records each complete() user message."""

    responses: list[Any]
    calls: list[list[Any]] = field(default_factory=list)

    def role_config(self, role: str) -> Any:
        return type("RoleCfg", (), {"context_window": 200_000, "max_output_tokens": 4096})()

    async def complete(
        self,
        role: str,
        messages: list[Any],
        *,
        run_id: str,
        attempt_id: str | None = None,
        tools: Any = None,
        temperature: float | None = None,
    ) -> Any:
        self.calls.append(list(messages))
        return self.responses.pop(0)


def _tc(id_: str, name: str, arguments_json: str) -> Any:
    from girder.models.gateway import ModelToolCall

    return ModelToolCall(id=id_, name=name, arguments_json=arguments_json)


def _resp(calls: list[Any] | None = None, content: str | None = None) -> Any:
    from girder.models.gateway import ModelResponse, Usage

    return ModelResponse(
        content=content,
        tool_calls=calls or [],
        finish_reason="tool_calls" if calls else "stop",
        usage=Usage(),
        role="tier2",
        model_id="fake",
        provider="fake",
    )


def write_commit_complete() -> list[Any]:
    """Standard compliant attempt: write in scope, commit, declare done."""
    return [
        _resp(
            calls=[
                _tc(
                    "1",
                    "write_file",
                    '{"path":"src/app.py","content":"def greet():\\n    return 1\\n"}',
                )
            ]
        ),
        _resp(calls=[_tc("2", "run_command", '{"cmd":"git add -A && git commit -m work"}')]),
        _resp(calls=[_tc("3", "mark_task_complete", '{"summary":"added greet"}')]),
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

    def engine(self, gateway: FakeGateway) -> TaskEngine:
        return TaskEngine(
            db=self.db,
            gateway=gateway,  # type: ignore[arg-type]
            sandbox=self.sandbox,
            settings=self.settings,
            redactor=Redactor(),
            notifier=self.notifier,  # type: ignore[arg-type]
            project=self.project,
            repo_path=self.repo_path,
            worktree_base=self.wt_base,
        )


async def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = await run_host_cmd(["git", "-C", str(cwd), *args], check=check, timeout_s=30)
    return result.stdout


@pytest.fixture
async def harness(db: Database, tmp_path: Path) -> AsyncIterator[Harness]:
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

    project = await repo.create_project(db, "p", str(repo_path))
    run = await repo.create_run(db, project.id, "intent", "run/eng1", 5.0)
    await seed_run_status(db, run.id, RunStatus.ACTIVE.value)
    run = await repo.get_run(db, run.id)
    assert run is not None
    # run branch exists at HEAD
    await _git(repo_path, "branch", run.branch)

    settings = Settings(
        project=ProjectConfig(test_directories=["tests"]),
        limits=LimitsConfig(
            task_max_attempts=2, attempt_max_turns=6, attempt_wallclock_s=60
        ),
        sandbox=SandboxNetwork(),
    )
    wt_base = tmp_path / "wt"
    sandbox = ScriptSandbox(suite_results=[], suite_xml=GREEN_XML)
    yield Harness(
        db=db,
        project=project,
        run=run,
        repo_path=repo_path,
        wt_base=wt_base,
        sandbox=sandbox,
        notifier=FakeNotifier(),
        settings=settings,
    )


async def _seed_task(h: Harness, *, scope_globs: list[str] | None = None) -> Task:
    wave = await repo.get_or_create_wave0(h.db, h.run.id)
    return await repo.create_task(
        h.db,
        wave.id,
        1,
        "Do the thing",
        TaskType.CODE_CHANGE,
        scope_globs=scope_globs or ["src/**"],
        spec_slice_md="## Task\n\nDo the thing.\n",
    )


async def _attempt_rows(db: Database, task_id: str) -> list[dict[str, Any]]:
    return await db.fetchall(
        "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt_num", (task_id,)
    )


async def test_happy_path_completes_and_merges(harness: Harness) -> None:
    h = harness
    task = await _seed_task(h)
    gateway = FakeGateway(responses=write_commit_complete())
    tip_before = await BranchOps(h.repo_path).run_branch_tip(h.run.branch)

    outcome = await h.engine(gateway).execute_task(h.run, task)
    assert outcome == TaskOutcome("completed", outcome.detail)
    assert outcome.detail is not None  # merged commit sha

    fresh = await repo.get_task(h.db, task.id)
    assert fresh is not None and fresh.status is TaskStatus.COMPLETED
    assert fresh.test_content_hash is not None  # Layer-2 anchor persisted

    attempts = await _attempt_rows(h.db, task.id)
    assert len(attempts) == 1 and attempts[0]["status"] == AttemptStatus.SUCCEEDED.value

    # run branch tip advanced to the merged commit
    tip_after = await BranchOps(h.repo_path).run_branch_tip(h.run.branch)
    assert tip_after == outcome.detail and tip_after != tip_before

    # container killed, worktree pruned (dir gone + row state)
    assert h.sandbox.killed
    rows = await repo.list_worktrees(h.db)
    assert rows and rows[0].state is WorktreeState.PRUNED
    assert not list(h.wt_base.rglob(f"**/{task.id}"))

    # events: task_completed + state transitions
    events = await h.db.fetchall(
        "SELECT event_type FROM agent_events WHERE run_id = ?", (h.run.id,)
    )
    types = {e["event_type"] for e in events}
    assert "task_completed" in types
    # no integrity findings
    viols = await h.db.fetchall("SELECT * FROM integrity_violations")
    assert viols == []


async def test_suite_red_then_green_retries_with_guidance(harness: Harness) -> None:
    h = harness
    task = await _seed_task(h)
    h.sandbox = ScriptSandbox(
        suite_results=[ExecResult(1, "assert 1 == 2\nFAILED tests/test_p.py::test_bad", ""),
                       ExecResult(0, "", "")],
        suite_xml=RED_XML,
    )

    responses = [*write_commit_complete(), *write_commit_complete()]
    gateway = FakeGateway(responses=responses)
    outcome = await h.engine(gateway).execute_task(h.run, task)
    assert outcome.kind == "completed"

    attempts = await _attempt_rows(h.db, task.id)
    assert len(attempts) == 2
    assert attempts[0]["status"] == AttemptStatus.FAILED.value
    assert attempts[1]["status"] == AttemptStatus.SUCCEEDED.value
    fresh = await repo.get_task(h.db, task.id)
    assert fresh is not None and fresh.attempts_used == 2

    # second attempt saw the redacted failure tail as trusted guidance
    second_user = next(
        m for m in gateway.calls[3] if getattr(m, "role", "") == "user"  # first msg of attempt 2
    )
    assert "Previous attempt failed verification" in second_user.content
    assert "assert 1 == 2" in second_user.content

    events = await h.db.fetchall(
        "SELECT event_type FROM agent_events WHERE run_id = ?", (h.run.id,)
    )
    assert any(e["event_type"] == "verify_failed" for e in events)


async def test_attempts_exhausted_fails_task_and_notifies(harness: Harness) -> None:
    h = harness
    task = await _seed_task(h)
    h.sandbox = ScriptSandbox(
        suite_results=[ExecResult(1, "", "fail"), ExecResult(1, "", "fail")],
        suite_xml=RED_XML,
    )
    gateway = FakeGateway(
        responses=[*write_commit_complete(), *write_commit_complete(), *write_commit_complete()]
    )
    outcome = await h.engine(gateway).execute_task(h.run, task)
    assert outcome.kind == "failed"

    fresh = await repo.get_task(h.db, task.id)
    assert fresh is not None and fresh.status is TaskStatus.FAILED
    assert fresh.attempts_used == 2  # task_max_attempts
    assert h.notifier.calls, "failure must notify"
    # run branch never moved
    tip = await BranchOps(h.repo_path).run_branch_tip(h.run.branch)
    head = (await _git(h.repo_path, "rev-parse", "main")).strip()
    assert tip == head


async def test_integrity_violation_fails_without_retry(harness: Harness) -> None:
    h = harness
    # code_change task whose scope INCLUDES tests/**: the write executes, but
    # the mechanical audit still flags the test-signal change (§8.2).
    task = await _seed_task(h, scope_globs=["src/**", "tests/**"])
    h.sandbox = ScriptSandbox(suite_results=[ExecResult(0, "", "")], suite_xml=GREEN_XML)
    gateway = FakeGateway(
        responses=[
            _resp(
                calls=[
                    _tc(
                        "1",
                        "write_file",
                        '{"path":"tests/new_test.py",'
                        '"content":"def test_x():\\n    assert True\\n"}',
                    )
                ]
            ),
            _resp(calls=[_tc("2", "run_command", '{"cmd":"git add -A && git commit -m sneaky"}')]),
            _resp(calls=[_tc("3", "mark_task_complete", '{"summary":"done"}')]),
        ]
    )
    tip_before = await BranchOps(h.repo_path).run_branch_tip(h.run.branch)
    outcome = await h.engine(gateway).execute_task(h.run, task)
    assert outcome.kind == "integrity_violation"

    attempts = await _attempt_rows(h.db, task.id)
    assert len(attempts) == 1  # NO retry
    assert attempts[0]["status"] == AttemptStatus.INTEGRITY_VIOLATION.value
    fresh = await repo.get_task(h.db, task.id)
    assert fresh is not None and fresh.status is TaskStatus.FAILED

    run_row = await repo.get_run(h.db, h.run.id)
    assert run_row is not None and run_row.integrity_violations >= 1
    kinds = await h.db.fetchall("SELECT kind FROM integrity_violations")
    kinds_set = {v["kind"] for v in kinds}
    assert "test_path_modified" in kinds_set
    assert "content_hash_mismatch" in kinds_set

    tip_after = await BranchOps(h.repo_path).run_branch_tip(h.run.branch)
    assert tip_after == tip_before  # never merged
    assert h.notifier.calls


async def test_amendment_handoff_and_resume(harness: Harness) -> None:
    h = harness
    # a frozen proposal on the run branch so `approved` can re-freeze
    proposal_path = h.repo_path / "openspec" / "proposals"
    proposal_path.mkdir(parents=True)
    (proposal_path / f"{h.run.id}.md").write_text(PROPOSAL)
    await _git(h.repo_path, "add", "-A")
    await _git(
        h.repo_path,
        "-c", "user.name=girder-test", "-c", "user.email=test@girder.local",
        "commit", "-m", "freeze proposal",
    )
    await _git(h.repo_path, "update-ref", f"refs/heads/{h.run.branch}", "HEAD")

    task = await _seed_task(h)
    amendment_gateway = FakeGateway(
        responses=[
            _resp(
                calls=[
                    _tc(
                        "1",
                        "request_spec_amendment",
                        '{"reason":"spec conflicts with reality","suggested_change":"use y"}',
                    )
                ]
            )
        ]
    )
    outcome = await h.engine(amendment_gateway).execute_task(h.run, task)
    assert outcome.kind == "amendment_pending"

    run_row = await repo.get_run(h.db, h.run.id)
    task_row = await repo.get_task(h.db, task.id)
    attempts = await _attempt_rows(h.db, task.id)
    assert run_row is not None and run_row.status is RunStatus.AWAITING_AMENDMENT
    assert task_row is not None and task_row.status is TaskStatus.AWAITING_AMENDMENT
    assert attempts[0]["status"] == AttemptStatus.AMENDMENT_REQUESTED.value

    amendment = await repo.get_pending_amendment(h.db, h.run.id)
    assert amendment is not None
    from girder.specs.amendment import resolve_amendment

    resolution = await resolve_amendment(
        h.db, project=h.project, run=h.run, amendment=amendment,
        decision="approved", notifier=None,
    )
    assert resolution.task_status == TaskStatus.RUNNING.value

    # resume: engine re-executes the (now running) task with a compliant agent
    resume_gateway = FakeGateway(responses=write_commit_complete())
    h.sandbox = ScriptSandbox(suite_results=[ExecResult(0, "", "")], suite_xml=GREEN_XML)
    outcome = await h.engine(resume_gateway).execute_task(h.run, task)
    assert outcome.kind == "completed"
    fresh = await repo.get_task(h.db, task.id)
    assert fresh is not None and fresh.status is TaskStatus.COMPLETED
    # the amended slice text rode along into the resumed attempt
    resumed_user = next(m for m in resume_gateway.calls[0] if getattr(m, "role", "") == "user")
    assert "use y" in resumed_user.content


async def test_amendment_reject_guidance_reaches_resumed_attempt(harness: Harness) -> None:
    h = harness
    task = await _seed_task(h)
    amendment_gateway = FakeGateway(
        responses=[
            _resp(
                calls=[
                    _tc(
                        "1",
                        "request_spec_amendment",
                        '{"reason":"spec conflicts with reality","suggested_change":"use y"}',
                    )
                ]
            )
        ]
    )
    outcome = await h.engine(amendment_gateway).execute_task(h.run, task)
    assert outcome.kind == "amendment_pending"

    amendment = await repo.get_pending_amendment(h.db, h.run.id)
    assert amendment is not None
    from girder.specs.amendment import resolve_amendment

    resolution = await resolve_amendment(
        h.db, project=h.project, run=h.run, amendment=amendment,
        decision="rejected", guidance="proceed without the change", notifier=None,
    )
    assert resolution.task_status == TaskStatus.RUNNING.value

    # resume: engine must relay the rejection guidance as trusted steering
    resume_gateway = FakeGateway(responses=write_commit_complete())
    h.sandbox = ScriptSandbox(suite_results=[ExecResult(0, "", "")], suite_xml=GREEN_XML)
    outcome = await h.engine(resume_gateway).execute_task(h.run, task)
    assert outcome.kind == "completed"
    resumed_user = next(m for m in resume_gateway.calls[0] if getattr(m, "role", "") == "user")
    assert "proceed without the change" in resumed_user.content
    assert "[TRUSTED] Steering directive (user-authored):" in resumed_user.content


async def test_no_rejected_amendment_means_no_synthetic_guidance(harness: Harness) -> None:
    h = harness
    task = await _seed_task(h)
    gateway = FakeGateway(responses=write_commit_complete())
    h.sandbox = ScriptSandbox(suite_results=[ExecResult(0, "", "")], suite_xml=GREEN_XML)
    outcome = await h.engine(gateway).execute_task(h.run, task)
    assert outcome.kind == "completed"
    first_user = next(m for m in gateway.calls[0] if getattr(m, "role", "") == "user")
    assert "Steering directive" not in first_user.content


async def test_budget_exhaustion_stops_before_any_dispatch(harness: Harness) -> None:
    import httpx

    from girder.budget.guard import BudgetGuard
    from girder.config import ModelRole, Secrets
    from girder.models.gateway import ModelGateway

    h = harness
    task = await _seed_task(h)

    def _role(role_name: str) -> ModelRole:
        return ModelRole(
            role=role_name, provider="openrouter", model="m",
            price_in_per_mtok=30.0, price_out_per_mtok=60.0,
        )

    settings = h.settings.model_copy(
        update={"models": type(h.settings.models)(
            roles=[_role("tier1"), _role("tier2"), _role("tier3")]
        )}
    )

    class NoDispatchTransport(httpx.MockTransport):
        def __init__(self) -> None:
            super().__init__(self._handle)
            self.calls = 0

        def _handle(self, request: httpx.Request) -> httpx.Response:
            self.calls += 1
            raise AssertionError("no HTTP may be dispatched under a zero cap")

    transport = NoDispatchTransport()
    gateway = ModelGateway(
        settings,
        Secrets(models_openrouter_api_key="sk-test-nonsecret"),
        h.db,
        Redactor(),
        BudgetGuard(h.db),
        None,
        transport=transport,
    )
    # zero the cap: any pre-flight estimate must be denied
    await h.db.execute(
        "UPDATE runs SET budget_cap_usd = 0.0 WHERE id = ?", (h.run.id,)
    )
    await h.db.conn.commit()

    engine = TaskEngine(
        db=h.db,
        gateway=gateway,
        sandbox=h.sandbox,
        settings=settings,
        redactor=Redactor(),
        notifier=None,
        project=h.project,
        repo_path=h.repo_path,
        worktree_base=h.wt_base,
    )
    outcome = await engine.execute_task(h.run, task)
    assert outcome.kind == "budget_exhausted"
    assert transport.calls == 0
    attempts = await _attempt_rows(h.db, task.id)
    assert attempts[0]["status"] == AttemptStatus.BUDGET_FROZEN.value
    run_row = await repo.get_run(h.db, h.run.id)
    assert run_row is not None and run_row.status is RunStatus.BUDGET_EXHAUSTED


async def test_uncommitted_attempt_retries_with_commit_guidance(harness: Harness) -> None:
    """§5.2: forgetting to commit is an ordinary attempt failure, retried with
    commit guidance — never an integrity violation."""
    h = harness
    h.settings.sandbox.python_bin = "py3-custom"
    task = await _seed_task(h)
    no_commit_responses = [
        _resp(
            calls=[
                _tc(
                    "1",
                    "write_file",
                    '{"path":"src/app.py","content":"def greet():\\n    return 1\\n"}',
                )
            ]
        ),
        _resp(calls=[_tc("3", "mark_task_complete", '{"summary":"done"}')]),
    ]
    gateway = FakeGateway(responses=[*no_commit_responses, *write_commit_complete()])
    outcome = await h.engine(gateway).execute_task(h.run, task)
    assert outcome.kind == "completed"

    attempts = await _attempt_rows(h.db, task.id)
    assert len(attempts) == 2
    assert attempts[0]["status"] == AttemptStatus.FAILED.value
    assert attempts[1]["status"] == AttemptStatus.SUCCEEDED.value

    # attempt 2's prompt carried the commit guidance
    second_user = next(
        m for m in gateway.calls[2] if getattr(m, "role", "") == "user"  # attempt 1 = 2 msgs
    )
    assert "git add -A && git commit" in second_user.content

    # NOT an integrity violation: no rows, counter untouched
    viols = await h.db.fetchall("SELECT * FROM integrity_violations")
    assert viols == []
    run_row = await repo.get_run(h.db, h.run.id)
    assert run_row is not None and run_row.integrity_violations == 0

    # the verify suite ran under the configured interpreter
    assert h.sandbox.suite_cmds and h.sandbox.suite_cmds[0][0] == "py3-custom"


async def test_merge_refusal_after_passed_audit_is_ordinary_retry(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    task = await _seed_task(h)
    original = BranchOps.audit_gated_merge
    calls = [0]

    async def _refuse(self: BranchOps, **kwargs: object) -> object:
        from girder.gitops.branch import MergeResult

        calls[0] += 1
        if calls[0] == 1:  # refuse only the first attempt; let the retry merge
            return MergeResult(False, None, "unexpected refusal")
        return await original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(BranchOps, "audit_gated_merge", _refuse)
    gateway = FakeGateway(responses=[*write_commit_complete(), *write_commit_complete()])
    outcome = await h.engine(gateway).execute_task(h.run, task)
    assert outcome.kind == "completed"  # attempt 2 merged for real

    attempts = await _attempt_rows(h.db, task.id)
    assert len(attempts) == 2
    assert attempts[0]["status"] == AttemptStatus.FAILED.value
    events = await h.db.fetchall(
        "SELECT event_type FROM agent_events WHERE run_id = ?", (h.run.id,)
    )
    assert any(e["event_type"] == "merge_refused" for e in events)
    viols = await h.db.fetchall("SELECT * FROM integrity_violations")
    assert viols == []


class NeverCompletingGateway(FakeGateway):
    """Gateway whose complete() hangs forever (until the task is cancelled)."""

    async def complete(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(list(args[-1]) if args else [])
        await asyncio.sleep(3600)


async def test_cancellation_kills_container_and_leaves_status_to_pump(
    harness: Harness,
) -> None:
    """Cancellation protocol: on CancelledError the engine kills the attempt's
    container and re-raises with NO DB status writes — the run pump owns the
    terminal transition."""
    h = harness
    task = await _seed_task(h)
    gateway = NeverCompletingGateway(responses=[])
    engine = h.engine(gateway)
    pending = asyncio.create_task(engine.execute_task(h.run, task))
    try:
        for _ in range(200):
            if gateway.calls:
                break
            await asyncio.sleep(0.05)
        assert gateway.calls, "agent turn never started"
        await asyncio.sleep(0.05)  # settle inside the attempt coroutine
        assert h.sandbox.started
        container = h.sandbox.started[0].name
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    finally:
        if not pending.done():
            pending.cancel()
            with suppress(asyncio.CancelledError):
                await pending

    assert container in h.sandbox.killed
    row = (await _attempt_rows(h.db, task.id))[0]
    assert row["status"] == AttemptStatus.RUNNING.value  # unchanged by task_engine
    assert row["ended_at"] is None
    fresh = await repo.get_task(h.db, task.id)
    assert fresh is not None and fresh.status is TaskStatus.RUNNING


async def test_multi_test_dir_project_gets_one_ro_snapshot_mount_per_dir(
    harness: Harness,
) -> None:
    """Issue: Layer 1 must not be silently skipped for multi-test-dir projects —
    one RO shadow mount per configured test directory, each backed by a
    materialized snapshot."""
    h = harness
    (h.repo_path / "spec_tests").mkdir()
    (h.repo_path / "spec_tests" / "test_s.py").write_text("def test_s():\n    assert True\n")
    await _git(h.repo_path, "add", "-A")
    await _git(h.repo_path, "commit", "-m", "second test dir")
    head = (await _git(h.repo_path, "rev-parse", "HEAD")).strip()
    await _git(h.repo_path, "update-ref", f"refs/heads/{h.run.branch}", head)
    h.settings.project.test_directories = ["tests", "spec_tests"]

    class RecordingSandbox(ScriptSandbox):
        """Records each started spec while the snapshot dirs still exist."""

        def __init__(self) -> None:
            super().__init__(suite_results=[], suite_xml=GREEN_XML)
            self.specs: list[ContainerSpec] = []

        async def start(self, spec: ContainerSpec) -> str:
            self.specs.append(spec)
            for host in spec.ro_mounts:
                assert await asyncio.to_thread(Path(host).is_dir), f"missing snapshot {host}"
            return await super().start(spec)

    h.sandbox = RecordingSandbox()
    task = await _seed_task(h)
    gateway = FakeGateway(responses=write_commit_complete())
    outcome = await h.engine(gateway).execute_task(h.run, task)
    assert outcome.kind == "completed"

    spec = h.sandbox.specs[0]
    assert len(spec.ro_mounts) == 2
    for host, container in spec.ro_mounts.items():
        snap = Path(host)
        assert snap.parent.name.startswith("girder-snap-")
        assert container.startswith("/workspace/")
        rel = container.removeprefix("/workspace/")
        assert rel in ("tests", "spec_tests")
        # archive keeps the path prefix, so the shadow source is <snap>/<dir>
        assert snap.name == rel
    # both configured test dirs are shadowed
    assert set(spec.ro_mounts.values()) == {"/workspace/tests", "/workspace/spec_tests"}
    # snapshots cleaned up after the attempt
    leftovers = [
        host
        for host in spec.ro_mounts
        if await asyncio.to_thread(Path(host).parent.exists)
    ]
    assert leftovers == []


async def test_empty_test_dirs_logs_layer1_warning(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    h = harness
    h.settings.project.test_directories = []
    task = await _seed_task(h)
    gateway = FakeGateway(responses=write_commit_complete())
    with caplog.at_level(logging.WARNING, logger="girder.orchestrator.task_engine"):
        outcome = await h.engine(gateway).execute_task(h.run, task)
    assert outcome.kind == "completed"
    assert "Layer 1" in caplog.text
    assert h.sandbox.started[0].ro_mounts == {}
