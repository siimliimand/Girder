"""Unit tests for the agent turn loop (WP 3.2): outcomes, caps, gating."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from girder.agent.runtime import AgentRuntime
from girder.agent.tools import TOOL_SCHEMAS
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
# planning_turns=0 keeps the WP 8.1 planning phase out of the way for the
# pre-existing tests; the planning tests below override it explicitly.
LIMITS = LimitsConfig(attempt_max_turns=3, planning_turns=0)


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
        python_bin="python3",
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
    # the violating write never reached the sandbox (only the terminal gate's
    # read-only dirty probe may run)
    assert all(cmd[:2] == ["git", "status"] for _, cmd, _ in sandbox.execs)
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


async def test_repeated_compaction_retains_system_and_task_brief(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    """§8.5: messages[:2] is always [system, task brief] across any number of
    compactions; scratchpads live in the tail and collapse to exactly ONE."""
    _db, _run_id, _attempt, _task = seeded

    class _SmallWindowGateway(FakeGateway):
        def role_config(self, role: str) -> Any:
            cfg = super().role_config(role)
            cfg.context_window = 1000  # compact above ~700 estimated tokens
            return cfg

    runtime = _runtime(seeded, _SmallWindowGateway([]), FakeSandbox())
    system_text = "SYSTEM INVARIANTS"
    brief_text = "TASK BRIEF with FROZEN SPEC SLICE: immutable-marker"
    messages = [
        Message(role="system", content=system_text),
        Message(role="user", content=brief_text),
    ]

    def _filler(n: int) -> Message:
        return Message(
            role="user",
            content='<untrusted-data source="read_file:src/big.py">\n'
            + ("X" * 600 * n)
            + "</untrusted-data>",
        )

    for _round in range(3):
        for _ in range(3):
            messages.append(_filler(5))
        messages = runtime._maybe_compact(messages)
        assert messages[0].content == system_text
        assert messages[1].content == brief_text
        assert "immutable-marker" in messages[1].content
        scratchpads = [m for m in messages if m.content.startswith("[TRUSTED] Scratchpad")]
        assert len(scratchpads) == 1
        assert messages[2] is scratchpads[0]


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
    assert tools is not None and len(tools) == len(TOOL_SCHEMAS)
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


async def test_dirty_worktree_bounces_terminal_until_clean(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    """mark_task_complete on a dirty tree is bounced with a commit reminder
    (verify's leftover audit would kill the attempt otherwise); after the
    commit, completion is accepted. Dogfood fdee1ec1: 3/3 attempts lost to a
    skipped commit step."""
    db, run_id, _attempt, _ = seeded
    sandbox = FakeSandbox(
        results=[
            ExecResult(0, " M README.md\n", ""),  # probe: dirty
            ExecResult(0, "", ""),                # git add -A && git commit
            ExecResult(0, "", ""),                # probe: clean
        ]
    )
    gateway = FakeGateway(
        responses=[
            _resp(calls=[_tc("1", "mark_task_complete", '{"summary":"done"}')]),
            _resp(
                calls=[_tc("2", "run_command", '{"cmd":"git add -A && git commit -m work"}')]
            ),
            _resp(calls=[_tc("3", "mark_task_complete", '{"summary":"done"}')]),
        ]
    )

    outcome = await _runtime(seeded, gateway, sandbox).execute_attempt(spec_slice="s")

    assert outcome.status == "succeeded"
    assert outcome.turns_used == 3
    # the bounce reached the model before the commit turn
    second_turn_messages = gateway.calls[1][1]
    assert any("ATTEMPT NOT ACCEPTED" in m.content for m in second_turn_messages)
    # and the bounce is observable in the audit trail
    events = await db.fetchall(
        "SELECT event_type FROM agent_events WHERE run_id = ?", (run_id,)
    )
    assert any(e["event_type"] == "terminal_bounced" for e in events)


async def test_dirty_probe_failure_fails_open(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    """A failed probe (not a dirty tree) accepts completion — verify's
    leftover audit stays the backstop; a flaky probe must not wedge attempts."""
    _db, _run_id, _attempt, _ = seeded
    sandbox = FakeSandbox(results=[ExecResult(1, "", "fatal: not a git repository")])
    gateway = FakeGateway(
        responses=[_resp(calls=[_tc("1", "mark_task_complete", '{"summary":"x"}')])]
    )

    outcome = await _runtime(seeded, gateway, sandbox).execute_attempt(spec_slice="s")

    assert outcome.status == "succeeded"


# ------------------------------------------------------- WP 8.1 planning phase

PLANNING_LIMITS = LimitsConfig(attempt_max_turns=3, planning_turns=5)


def _planning_runtime(
    seeded: tuple[Database, str, Attempt, Task],
    gateway: FakeGateway,
    sandbox: SandboxEngine,
    limits: LimitsConfig = PLANNING_LIMITS,
) -> AgentRuntime:
    db, run_id, attempt, task = seeded
    return AgentRuntime(
        gateway=gateway,  # type: ignore[arg-type]
        sandbox=sandbox,
        container="ctr",
        scopes=SCOPES,
        limits=limits,
        python_bin="python3",
        redactor=Redactor(),
        db=db,
        run_id=run_id,
        attempt=attempt,
        task=task,
    )


async def test_planning_phase_holds_write_tools_without_recording_them(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    """WP 8.1 (R-SP8-1): during the planning phase a write tool returns the
    synthetic planning result and the registry NEVER sees the call — no
    tool_calls row, held or otherwise, and nothing reaches the sandbox."""
    db, _run_id, attempt, _ = seeded
    gateway = FakeGateway(
        [
            _resp(calls=[_tc("1", "write_file", '{"path":"src/a.py","content":"x"}')]),
            _resp(calls=[_tc("2", "mark_task_complete", '{"summary":"done"}')]),
        ]
    )
    sandbox = FakeSandbox()
    outcome = await _planning_runtime(seeded, gateway, sandbox).execute_attempt(spec_slice="s")
    assert outcome.status == "succeeded"
    # the write never executed — only the terminal gate's read-only dirty
    # probe may touch the sandbox
    assert all(cmd[:2] == ["git", "status"] for _, cmd, _ in sandbox.execs)
    rows = await repo.list_tool_calls_for_attempt(db, attempt.id)
    assert [r["tool_name"] for r in rows] == ["mark_task_complete"]  # no write row at all
    # the model got the planning message as a plain tool result
    second_turn_messages = gateway.calls[1][1]
    assert any(
        "[planning phase] Write tools are not available" in m.content
        for m in second_turn_messages
        if m.role == "user"
    )


async def test_planning_hold_does_not_taint_the_integrity_ledger(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    """The planning hold must be invisible to the held-call machinery: a
    mark_task_complete in the SAME turn as a planning-held write is a clean
    completion (held=False), and count_held_tool_calls stays at zero."""
    db, _run_id, attempt, _ = seeded
    gateway = FakeGateway(
        [
            _resp(
                calls=[
                    _tc("1", "write_file", '{"path":"src/a.py","content":"x"}'),
                    _tc("2", "mark_task_complete", '{"summary":"done"}'),
                ]
            ),
        ]
    )
    outcome = await _planning_runtime(seeded, gateway, FakeSandbox()).execute_attempt(
        spec_slice="s"
    )
    assert outcome.status == "succeeded"  # NOT integrity_violation
    assert await repo.count_held_tool_calls(db, attempt.id) == 0
    rows = await db.fetchall("SELECT kind FROM integrity_violations")
    assert rows == []


async def test_plan_block_unlocks_write_tools_same_turn(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    """A PLAN block in the response unlocks write tools immediately — the
    planning budget is a ceiling, not a delay."""
    _db, _run_id, _attempt, _ = seeded
    gateway = FakeGateway(
        [
            _resp(
                content="PLAN:\n- Read: src/a.py\n- Write: src/b.py\n- Test: pytest",
                calls=[_tc("1", "write_file", '{"path":"src/b.py","content":"x"}')],
            ),
            _resp(calls=[_tc("2", "mark_task_complete", '{"summary":"done"}')]),
        ]
    )
    sandbox = FakeSandbox(results=[ExecResult(0, "", "")])
    outcome = await _planning_runtime(seeded, gateway, sandbox).execute_attempt(spec_slice="s")
    assert outcome.status == "succeeded"
    assert any(cmd[0] == "mkdir" for _, cmd, _ in sandbox.execs)  # the write ran


async def test_planning_turns_zero_disables_the_phase(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    _db, _run_id, _attempt, _ = seeded
    gateway = FakeGateway(
        [
            _resp(calls=[_tc("1", "write_file", '{"path":"src/a.py","content":"x"}')]),
            _resp(calls=[_tc("2", "mark_task_complete", '{"summary":"done"}')]),
        ]
    )
    sandbox = FakeSandbox(results=[ExecResult(0, "", "")])
    outcome = await _planning_runtime(
        seeded, gateway, sandbox, limits=LimitsConfig(attempt_max_turns=3, planning_turns=0)
    ).execute_attempt(spec_slice="s")
    assert outcome.status == "succeeded"
    assert any(cmd[0] == "mkdir" for _, cmd, _ in sandbox.execs)


async def test_planning_budget_announced_in_system_prompt(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    _db, _run_id, _attempt, _ = seeded
    gateway = FakeGateway([_resp(calls=[_tc("1", "mark_task_complete", '{"summary":"s"}')])])
    await _planning_runtime(seeded, gateway, FakeSandbox()).execute_attempt(spec_slice="s")
    system = gateway.calls[0][1][0].content
    assert "REQUIRED PLANNING PHASE (turns 1-5)" in system
    assert "PLAN:" in system


# --------------------------------------------- WP 8.2 structured scratchpad


class _SmallWindowGateway(FakeGateway):
    def role_config(self, role: str) -> Any:
        cfg = super().role_config(role)
        cfg.context_window = 1000
        return cfg


async def test_compaction_refreshes_structured_scratchpad_without_distillation(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    """WP 8.2: with a structured scratchpad, compaction refreshes the index-2
    JSON block from registry-maintained state — messages[:2] survive verbatim
    and NO distillation (gateway) call happens."""
    _db, _run_id, _attempt, _task = seeded
    gateway = _SmallWindowGateway([])
    runtime = _planning_runtime(seeded, gateway, FakeSandbox(), limits=LIMITS)
    runtime._scratchpad.plan = "PLAN: write the thing"
    runtime._scratchpad.note_read("src/a.py")
    runtime._scratchpad.note_write("src/b.py")
    runtime._scratchpad.milestones.append("Half your turns are spent")

    system_text = "SYSTEM INVARIANTS"
    brief_text = "TASK BRIEF immutable-marker"
    messages = [
        Message(role="system", content=system_text),
        Message(role="user", content=brief_text),
        Message(
            role="user",
            content='<untrusted-data source="read_file:src/a.py">\n'
            + ("X" * 6000)
            + "</untrusted-data>",
        ),
    ]
    messages = runtime._maybe_compact(messages)
    assert gateway.calls == []  # no distillation model call
    assert messages[0].content == system_text
    assert messages[1].content == brief_text
    scratch = messages[2]
    assert scratch.role == "system"
    assert scratch.content.startswith("[TRUSTED] Scratchpad:")
    assert '"files_read": ["src/a.py"]' in scratch.content
    assert '"plan (untrusted model output, verbatim)": "PLAN: write the thing"' in scratch.content
    # second compaction refreshes in place — never two scratchpad messages
    messages.append(
        Message(
            role="user",
            content='<untrusted-data source="read_file:src/a.py">\n'
            + ("Y" * 6000)
            + "</untrusted-data>",
        )
    )
    messages = runtime._maybe_compact(messages)
    scratchpads = [m for m in messages if m.content.startswith("[TRUSTED] Scratchpad")]
    assert len(scratchpads) == 1 and messages[2] is scratchpads[0]


async def test_milestones_and_reads_land_in_the_scratchpad(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    """Milestone directives and executed reads are mirrored into the
    structured scratchpad."""
    _db, _run_id, _attempt, _ = seeded
    gateway = FakeGateway(
        [
            _resp(calls=[_tc("1", "read_file", '{"path":"src/a.py"}')]),
            _resp(content="still exploring"),
            _resp(content="still exploring"),
            _resp(content="still exploring"),
            _resp(content="still exploring"),
        ]
    )
    runtime = _planning_runtime(
        seeded, gateway, FakeSandbox(results=[ExecResult(0, "body\n", "")]),
        limits=LimitsConfig(attempt_max_turns=5, planning_turns=0),
    )
    outcome = await runtime.execute_attempt(spec_slice="s")
    assert outcome.status == "failed"  # ran out of turns
    pad = runtime._scratchpad
    assert "src/a.py" in pad.files_read
    assert any("Half your turns" in m for m in pad.milestones)
    assert any("Final turns" in m for m in pad.milestones)


async def test_prior_attempt_summary_lands_in_scratchpad(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    """WP 8.3 wiring: the retry brief passed as prior_attempt_summary is kept
    on the new attempt's structured scratchpad (and thus survives compaction)."""
    _db, _run_id, _attempt, _ = seeded
    gateway = FakeGateway([_resp(calls=[_tc("1", "mark_task_complete", '{"summary":"s"}')])])
    runtime = _planning_runtime(seeded, gateway, FakeSandbox(), limits=LIMITS)
    await runtime.execute_attempt(
        spec_slice="s", prior_attempt_summary="[TRUSTED] Retry brief (attempt 1 failed):"
    )
    assert runtime._scratchpad.prior_attempt_summary == (
        "[TRUSTED] Retry brief (attempt 1 failed):"
    )


async def test_planning_hold_never_laundered_scope_violation_still_recorded(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    """A write the scope gate would hold (tests/** is out of scope) stays a
    recorded integrity violation even during the planning phase — the
    planning hold only suppresses writes that would have been allowed."""
    db, _run_id, attempt, _ = seeded
    gateway = FakeGateway(
        [
            _resp(calls=[_tc("1", "write_file", '{"path":"tests/test_x.py","content":"boom"}')]),
            _resp(calls=[_tc("2", "mark_task_complete", '{"summary":"ok"}')]),
        ]
    )
    outcome = await _planning_runtime(seeded, gateway, FakeSandbox()).execute_attempt(
        spec_slice="s"
    )
    assert outcome.status == "succeeded"  # held but not terminal-turn-tainted
    rows = await repo.list_tool_calls_for_attempt(db, attempt.id)
    bad = next(r for r in rows if r["tool_name"] == "write_file")
    assert bad["scope_violation"] and bad["held"]
    assert await repo.count_held_tool_calls(db, attempt.id) == 1

# ------------------------------------------------- B1/B3/R1 review fixes


async def test_plan_only_turn_arms_the_unlock_latch(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    """B1: a compliant model emits the PLAN block as plain text with NO tool
    calls on turn 1 — that turn must still arm the latch and store the plan,
    so the turn-2 write executes instead of being held. A forced compaction
    afterwards re-injects the plan in the refreshed scratchpad message (with
    its B3 untrusted provenance label)."""
    _db, _run_id, _attempt, _ = seeded
    gateway = FakeGateway(
        [
            _resp(content="PLAN:\n- Read: src/a.py\n- Write: src/b.py\n- Test: pytest"),
            _resp(calls=[_tc("1", "write_file", '{"path":"src/b.py","content":"x"}')]),
            _resp(calls=[_tc("2", "mark_task_complete", '{"summary":"done"}')]),
        ]
    )
    sandbox = FakeSandbox(results=[ExecResult(0, "", "")])
    runtime = _planning_runtime(seeded, gateway, sandbox)
    runtime._context_window = 1000  # force should_compact on the tail below
    outcome = await runtime.execute_attempt(spec_slice="s")
    assert outcome.status == "succeeded"
    assert runtime._plan_emitted
    assert "PLAN:" in runtime._scratchpad.plan
    # the turn-2 write EXECUTED (mkdir plumbing), it was not held
    assert any(cmd[0] == "mkdir" for _, cmd, _ in sandbox.execs)

    # forced compaction: the plan survives in the refreshed [TRUSTED]
    # scratchpad message, labeled untrusted inside the JSON
    messages = [
        Message(role="system", content="SYSTEM INVARIANTS"),
        Message(role="user", content="TASK BRIEF"),
        Message(role="user", content="X" * 6000),
    ]
    messages = runtime._maybe_compact(messages)
    scratch = messages[2]
    assert scratch.role == "system" and scratch.content.startswith("[TRUSTED] Scratchpad:")
    assert "plan (untrusted model output, verbatim)" in scratch.content
    assert "PLAN:" in scratch.content


async def test_planning_hold_emits_planning_hold_event(
    seeded: tuple[Database, str, Attempt, Task],
) -> None:
    """R1/R3: every planning-phase hold fires a countable planning_hold
    agent_event carrying the tool and the turn number."""
    db, run_id, attempt, _ = seeded
    gateway = FakeGateway(
        [
            _resp(calls=[_tc("1", "write_file", '{"path":"src/a.py","content":"x"}')]),
            _resp(calls=[_tc("2", "mark_task_complete", '{"summary":"done"}')]),
        ]
    )
    outcome = await _planning_runtime(seeded, gateway, FakeSandbox()).execute_attempt(
        spec_slice="s"
    )
    assert outcome.status == "succeeded"
    rows = await db.fetchall(
        "SELECT payload_json FROM agent_events WHERE event_type = 'planning_hold'"
        " AND attempt_id = ?",
        (attempt.id,),
    )
    import json as _json

    payloads = [_json.loads(r["payload_json"]) for r in rows]
    assert payloads == [{"tool": "write_file", "turn": 1}]
    # run scoping matches the other runtime events
    assert all(r["payload_json"] for r in rows)
    del run_id
