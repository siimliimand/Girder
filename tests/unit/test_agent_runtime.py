"""Unit tests for the agent turn loop (WP 3.2): outcomes, caps, gating."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from girder.agent.runtime import AgentRuntime
from girder.config import LimitsConfig
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Attempt, Task, TaskType
from girder.guard.redact import Redactor
from girder.guard.scope import TaskScopes
from girder.models.gateway import Message, ModelResponse, ModelToolCall, Usage
from girder.sandbox.engine import ExecResult, SandboxEngine
from tests.unit.test_agent_tools import TASK, FakeSandbox


class FakeGateway:
    """Scripted responses; records every complete() call."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, list[Message], Any, str, str | None]] = []

    def role_config(self, role: str) -> Any:
        return type("RoleCfg", (), {"context_window": 200_000, "max_output_tokens": 4096})()

    async def complete(
        self,
        role: str,
        messages: list[Message],
        *,
        run_id: str,
        attempt_id: str | None = None,
        tools: Any = None,
        temperature: float | None = None,
    ) -> ModelResponse:
        self.calls.append((role, list(messages), tools, run_id, attempt_id))
        return self.responses.pop(0)


SCOPES = TaskScopes(write_globs=["src/**"], protected_globs=[".github/**"])
LIMITS = LimitsConfig(attempt_max_turns=3)


@pytest.fixture
async def seeded(db: Database) -> tuple[Database, str, Attempt, Task]:
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    task_row = await repo.create_task(db, wave.id, 1, "t", TaskType.CODE_CHANGE)
    attempt = await repo.create_attempt(db, task_row.id, "abc123")
    return db, run.id, attempt, TASK


def _tc(id: str, name: str, arguments_json: str) -> ModelToolCall:
    return ModelToolCall(id=id, name=name, arguments_json=arguments_json)


def _resp(
    content: str | None = None,
    calls: list[ModelToolCall] | None = None,
) -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=calls or [],
        finish_reason="tool_calls" if calls else "stop",
        usage=Usage(),
        role="tier2",
        model_id="fake",
        provider="fake",
    )


def _runtime(
    seeded: tuple[Database, str, Attempt, Task],
    gateway: FakeGateway,
    sandbox: SandboxEngine,
) -> AgentRuntime:
    db, run_id, attempt, task = seeded
    return AgentRuntime(
        gateway=gateway,  # type: ignore[arg-type]
        sandbox=sandbox,
        container="ctr",
        scopes=SCOPES,
        limits=LIMITS,
        redactor=Redactor(),
        db=db,
        run_id=run_id,
        attempt=attempt,
        task=task,
    )


async def test_happy_path_read_then_complete(seeded: tuple[Database, str, Attempt, Task]) -> None:
    db, _run_id, attempt, _ = seeded
    gateway = FakeGateway(
        [
            _resp(calls=[_tc("1", "read_file", '{"path":"src/a.py"}')]),
            _resp(
                content="all done",
                calls=[_tc("2", "mark_task_complete", '{"summary":"added parser"}')],
            ),
        ]
    )
    sandbox = FakeSandbox(results=[ExecResult(0, "file body\n", "")])
    outcome = await _runtime(seeded, gateway, sandbox).execute_attempt(spec_slice="s")
    assert outcome.status == "succeeded"
    assert outcome.summary == "added parser"
    assert outcome.turns_used == 2
    # the gateway saw tool results wrapped as untrusted data
    final_messages = gateway.calls[-1][1]
    assert any('<untrusted-data source="read_file:src/a.py">' in m.content for m in final_messages)
    # and the tool call rows were persisted
    rows = await repo.list_tool_calls_for_attempt(db, attempt.id)
    assert [r["tool_name"] for r in rows] == ["read_file", "mark_task_complete"]


async def test_turn_cap_fails_without_terminal_tool(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    _db, _run_id, _attempt, _ = seeded
    gateway = FakeGateway([_resp(content="thinking...") for _ in range(3)])
    outcome = await _runtime(seeded, gateway, FakeSandbox()).execute_attempt(spec_slice="s")
    assert outcome.status == "failed"
    assert "turn budget" in (outcome.failure_reason or "")
    assert outcome.turns_used == LIMITS.attempt_max_turns
    # every silent turn got the act-via-tools nudge
    for _role, messages, _tools, _r, _a in gateway.calls[1:]:
        assert any("must act via tools" in m.content for m in messages if m.role == "user")


async def test_deadline_prevents_any_gateway_call(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    _db, _run_id, _attempt, _ = seeded
    gateway = FakeGateway([_resp()])
    loop = asyncio.get_running_loop()
    runtime = _runtime(seeded, gateway, FakeSandbox())
    outcome = await runtime.execute_attempt(spec_slice="s", deadline_s=loop.time() - 1.0)
    assert outcome.status == "timeout"
    assert gateway.calls == []


async def test_scope_violation_holds_but_attempt_continues(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    db, _run_id, attempt, _ = seeded
    gateway = FakeGateway(
        [
            _resp(
                calls=[
                    _tc("1", "write_file", '{"path":"tests/test_x.py","content":"boom"}'),
                ]
            ),
            _resp(calls=[_tc("2", "mark_task_complete", '{"summary":"ok"}')]),
        ]
    )
    sandbox = FakeSandbox()
    outcome = await _runtime(seeded, gateway, sandbox).execute_attempt(spec_slice="s")
    assert outcome.status == "succeeded"  # the held call did not abort the attempt
    assert sandbox.execs == []  # the violating write never reached the sandbox
    rows = await repo.list_tool_calls_for_attempt(db, attempt.id)
    bad = next(r for r in rows if r["tool_name"] == "write_file")
    assert bad["scope_violation"] and bad["held"]
    violations = await db.fetchall("SELECT kind FROM integrity_violations")
    assert [v["kind"] for v in violations] == ["out_of_scope_write"]
    # the model was told the call was held
    final_messages = gateway.calls[-1][1]
    assert any("held for review" in m.content for m in final_messages)


async def test_terminal_turn_with_held_call_is_integrity_violation(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    """§8.5: mark_task_complete in the SAME turn as a held scope violation is
    not a clean completion — the attempt must fail without retry, and the
    integrity ledger row (written by the registry) must appear exactly once."""
    db, run_id, _attempt, _ = seeded
    gateway = FakeGateway(
        [
            _resp(
                calls=[
                    _tc("1", "write_file", '{"path":"src/a.py","content":"ok"}'),
                    _tc("2", "write_file", '{"path":"tests/test_x.py","content":"boom"}'),
                    _tc("3", "mark_task_complete", '{"summary":"done"}'),
                ]
            ),
        ]
    )
    sandbox = FakeSandbox()
    outcome = await _runtime(seeded, gateway, sandbox).execute_attempt(spec_slice="s")
    assert outcome.status == "integrity_violation"
    # the in-scope write executed (mkdir + write), nothing else ran
    assert len(sandbox.execs) == 2
    violations = await db.fetchall("SELECT kind FROM integrity_violations")
    assert [v["kind"] for v in violations] == ["out_of_scope_write"]  # exactly once
    assert await repo.get_latest_event(db, run_id, "attempt_finished") is not None


async def test_held_call_in_earlier_turn_then_clean_terminal_stays_succeeded(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    """A held call in an EARLIER turn followed later by a clean terminal turn
    is NOT tainted: it stays succeeded (surfaced via the ledger + gate)."""
    _db, _run_id, _attempt, _ = seeded
    gateway = FakeGateway(
        [
            _resp(calls=[_tc("1", "write_file", '{"path":"tests/test_x.py","content":"x"}')]),
            _resp(calls=[_tc("2", "mark_task_complete", '{"summary":"ok"}')]),
        ]
    )
    outcome = await _runtime(seeded, gateway, FakeSandbox()).execute_attempt(spec_slice="s")
    assert outcome.status == "succeeded"


async def test_malformed_arguments_yield_synthetic_error(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    _db, _run_id, _attempt, _ = seeded
    gateway = FakeGateway(
        [
            _resp(calls=[_tc("1", "read_file", "{not json")]),
            _resp(calls=[_tc("2", "mark_task_complete", '{"summary":"ok"}')]),
        ]
    )
    outcome = await _runtime(seeded, gateway, FakeSandbox()).execute_attempt(spec_slice="s")
    assert outcome.status == "succeeded"
    final_messages = gateway.calls[-1][1]
    assert any("malformed tool arguments" in m.content for m in final_messages)


async def test_spec_amendment_outcome(seeded: tuple[Database, str, Attempt, Task]) -> None:
    seeded_gateway = FakeGateway(
        [
            _resp(
                calls=[
                    _tc(
                        "1",
                        "request_spec_amendment",
                        '{"reason":"impossible","suggested_change":"swap x for y"}',
                    )
                ]
            )
        ]
    )
    outcome = await _runtime(seeded, seeded_gateway, FakeSandbox()).execute_attempt(spec_slice="s")
    assert outcome.status == "amendment_requested"
    assert outcome.amendment == ("impossible", "swap x for y")


async def test_guidance_and_schema_reach_the_gateway(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    _db, run_id, attempt, _ = seeded
    gateway = FakeGateway([_resp(calls=[_tc("1", "mark_task_complete", '{"summary":"s"}')])])
    await _runtime(seeded, gateway, FakeSandbox()).execute_attempt(
        spec_slice="slice-text", guidance="prefer small diffs"
    )
    role, messages, tools, seen_run, seen_attempt = gateway.calls[0]
    assert role == "tier2"
    assert seen_run == run_id and seen_attempt == attempt.id
    assert tools is not None and len(tools) == 9
    assert "[TRUSTED] Steering directive (user-authored):" in messages[1].content
    assert "slice-text" in messages[1].content
    # system message carries the untrusted-content framing rule
    assert "UNTRUSTED CONTENT RULE" in messages[0].content


async def test_prompts_persisted_per_turn_and_redacted(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    """plan.md Phase 5 task 5 / §8.6: every outgoing prompt is persisted
    (redacted) before the gateway call, one row per turn, in order."""
    db, run_id, attempt, _ = seeded
    secret = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"  # credential-shaped
    gateway = FakeGateway(
        [
            _resp(calls=[_tc("1", "read_file", '{"path":"src/a.py"}')]),
            _resp(
                content="all done",
                calls=[_tc("2", "mark_task_complete", '{"summary":"ok"}')],
            ),
        ]
    )
    sandbox = FakeSandbox(results=[ExecResult(0, f"token {secret} in output\n", "")])
    outcome = await _runtime(seeded, gateway, sandbox).execute_attempt(spec_slice="s")
    assert outcome.status == "succeeded"
    rows = await repo.list_attempt_prompts(db, attempt.id)
    assert [r.turn for r in rows] == [1, 2]
    assert all(r.role == "turn" for r in rows)
    assert all(r.run_id == run_id for r in rows)
    # turn 1 snapshot is the initial system+user prompt; turn 2 embeds the
    # tool result — stored redacted, never raw.
    assert "token" in rows[1].content_redacted
    assert secret not in rows[1].content_redacted
    assert "***[REDACTED:" in rows[1].content_redacted


async def test_prompt_persistence_failure_is_fail_open(
    seeded: tuple[Database, str, Attempt, Task],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prompt-persistence failure must never kill the attempt (§5.5)."""
    _db, _run_id, _attempt, _ = seeded
    gateway = FakeGateway([_resp(calls=[_tc("1", "mark_task_complete", '{"summary":"s"}')])])

    async def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("db offline")

    monkeypatch.setattr(repo, "record_attempt_prompt", boom)
    outcome = await _runtime(seeded, gateway, FakeSandbox()).execute_attempt(spec_slice="s")
    assert outcome.status == "succeeded"
    assert len(gateway.calls) == 1


class SlowGateway(FakeGateway):
    """FakeGateway whose complete() sleeps before answering."""

    def __init__(self, responses: list[ModelResponse], delay: float) -> None:
        super().__init__(responses)
        self.delay = delay

    async def complete(self, *args: Any, **kwargs: Any) -> ModelResponse:
        await asyncio.sleep(self.delay)
        return await super().complete(*args, **kwargs)


class SlowSandbox(FakeSandbox):
    """FakeSandbox whose exec() sleeps before returning."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay
        self.exec_calls = 0

    async def exec(
        self, name: str, cmd: list[str], *, timeout_s: float = 120.0, user: str | None = None
    ) -> ExecResult:
        self.exec_calls += 1
        await asyncio.sleep(self.delay)
        return ExecResult(0, "slow output\n", "")


async def test_gateway_overshoot_kills_turn_as_timeout(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    """§8.3.2: a single slow model call must not overshoot the ceiling."""
    _db, _run_id, _attempt, _ = seeded
    loop = asyncio.get_running_loop()
    gateway = SlowGateway([_resp()], delay=5.0)
    runtime = _runtime(seeded, gateway, FakeSandbox())
    outcome = await runtime.execute_attempt(spec_slice="s", deadline_s=loop.time() + 0.2)
    assert outcome.status == "timeout"
    assert "mid-turn" in (outcome.failure_reason or "")


async def test_tool_overshoot_ends_attempt_as_timeout(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    """§8.3.2: a tool running past the deadline ends the attempt as timeout
    and later tool calls in the same turn are never executed."""
    db, _run_id, attempt, _ = seeded
    loop = asyncio.get_running_loop()
    gateway = FakeGateway(
        [
            _resp(
                calls=[
                    _tc("1", "read_file", '{"path":"src/a.py"}'),
                    _tc("2", "read_file", '{"path":"src/b.py"}'),
                ]
            ),
            _resp(calls=[_tc("3", "mark_task_complete", '{"summary":"s"}')]),
        ]
    )
    sandbox = SlowSandbox(delay=5.0)
    runtime = _runtime(seeded, gateway, sandbox)
    outcome = await runtime.execute_attempt(spec_slice="s", deadline_s=loop.time() + 0.2)
    assert outcome.status == "timeout"
    assert sandbox.exec_calls == 1  # the second tool never ran
    # the cancelled call never reached persistence (no post-hoc row)
    rows = await repo.list_tool_calls_for_attempt(db, attempt.id)
    assert rows == []
