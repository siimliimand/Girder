"""WP-1.4 — FSM: legal/illegal edges, audit events, transition atomicity."""

from __future__ import annotations

import json

import pytest

from girder import fsm
from girder.db import repo
from girder.db.models import AttemptStatus, RunStatus, TaskStatus, TaskType
from girder.fsm import (
    _ATTEMPT_EDGES,
    _RUN_EDGES,
    _TASK_EDGES,
    InvalidTransition,
    current_status,
    transition,
    transition_attempt,
    transition_run,
    transition_task,
)

_seed_counter = 0


async def _seed(db):  # type: ignore[no-untyped-def]
    """project → run → wave → task → attempt, in default starting states."""
    global _seed_counter
    _seed_counter += 1
    project = await repo.create_project(db, f"p{_seed_counter}", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    task = await repo.create_task(
        db,
        wave.id,
        1,
        "Implement widget",
        TaskType.CODE_CHANGE,
        scope_globs=["src/widget.py"],
    )
    attempt = await repo.create_attempt(db, task.id, "abc123")
    return project, run, wave, task, attempt


# ------------------------------------------------------------------ happy paths


async def test_run_lifecycle_happy_path(db) -> None:  # type: ignore[no-untyped-def]
    _, run, _, _, _ = await _seed(db)
    path = [
        RunStatus.SPEC_PENDING,
        RunStatus.SPEC_APPROVED,
        RunStatus.BASELINE_RUNNING,
        RunStatus.ACTIVE,
        RunStatus.PR_OPEN,
        RunStatus.CI_RUNNING,
        RunStatus.CONFORMANCE_REVIEW,
        RunStatus.MERGED,
    ]
    for state in path:
        await transition_run(db, run.id, state)
    assert await repo.get_run(db, run.id)  # type: ignore[attr-defined]
    fresh = await repo.get_run(db, run.id)
    assert fresh is not None and fresh.status == RunStatus.MERGED


async def test_task_lifecycle_with_retry(db) -> None:  # type: ignore[no-untyped-def]
    _, _, _, task, _ = await _seed(db)
    for state in (
        TaskStatus.SCHEDULED,
        TaskStatus.RUNNING,
        TaskStatus.VERIFYING,
        TaskStatus.VERIFY_PASSED,
        TaskStatus.RETRY_SCHEDULED,
        TaskStatus.RUNNING,
        TaskStatus.VERIFYING,
        TaskStatus.VERIFY_PASSED,
        TaskStatus.COMPLETED,
    ):
        await transition_task(db, task.id, state)
    fresh = await repo.get_task(db, task.id)
    assert fresh is not None and fresh.status == TaskStatus.COMPLETED


async def test_attempt_lifecycle(db) -> None:  # type: ignore[no-untyped-def]
    _, _, _, _, attempt = await _seed(db)
    await transition_attempt(db, attempt.id, AttemptStatus.RUNNING)
    await transition_attempt(db, attempt.id, AttemptStatus.SUCCEEDED)
    fresh = await repo.get_attempt(db, attempt.id)
    assert fresh is not None and fresh.status == AttemptStatus.SUCCEEDED


# ------------------------------------------------------------------ illegal edges


async def test_illegal_transition_rejected(db) -> None:  # type: ignore[no-untyped-def]
    _, run, _, _, _ = await _seed(db)
    with pytest.raises(InvalidTransition, match="draft -> merged"):
        await transition_run(db, run.id, RunStatus.MERGED)


async def test_terminal_states_have_no_exits(db) -> None:  # type: ignore[no-untyped-def]
    _, run, _, task, attempt = await _seed(db)
    await transition_run(db, run.id, RunStatus.ABORTED)
    with pytest.raises(InvalidTransition):
        await transition_run(db, run.id, RunStatus.ACTIVE)

    for state in (TaskStatus.SCHEDULED, TaskStatus.RUNNING, TaskStatus.FAILED):
        await transition_task(db, task.id, state)
    with pytest.raises(InvalidTransition):
        await transition_task(db, task.id, TaskStatus.RUNNING)

    await transition_attempt(db, attempt.id, AttemptStatus.RUNNING)
    await transition_attempt(db, attempt.id, AttemptStatus.CRASHED)
    with pytest.raises(InvalidTransition):
        await transition_attempt(db, attempt.id, AttemptStatus.RUNNING)


async def test_every_declared_edge_is_legal_and_others_are_not(db) -> None:  # type: ignore[no-untyped-def]
    """Exhaustive walk: every edge in the tables executes, every non-edge raises."""
    from girder.db.models import AttemptStatus, RunStatus, TaskStatus

    # runs
    _, run, _, _, _ = await _seed(db)
    legal_run = {(f.value, t.value) for f, targets in _RUN_EDGES.items() for t in targets}
    all_runs = [s.value for s in RunStatus]
    for src in all_runs:
        for dst in all_runs:
            await _force_status(db, "runs", run.id, src)  # fresh source state each time
            if (src, dst) in legal_run:
                await transition(db, "run", run.id, dst)
            else:
                with pytest.raises(InvalidTransition):
                    await transition(db, "run", run.id, dst)

    # tasks
    _, _, _, task, _ = await _seed(db)
    legal_task = {(f.value, t.value) for f, targets in _TASK_EDGES.items() for t in targets}
    all_tasks = [s.value for s in TaskStatus]
    for src in all_tasks:
        for dst in all_tasks:
            await _force_status(db, "tasks", task.id, src)
            if (src, dst) in legal_task:
                await transition(db, "task", task.id, dst)
            else:
                with pytest.raises(InvalidTransition):
                    await transition(db, "task", task.id, dst)

    # attempts
    _, _, _, _, attempt = await _seed(db)
    legal_att = {(f.value, t.value) for f, targets in _ATTEMPT_EDGES.items() for t in targets}
    all_atts = [s.value for s in AttemptStatus]
    for src in all_atts:
        for dst in all_atts:
            await _force_status(db, "attempts", attempt.id, src)
            if (src, dst) in legal_att:
                await transition(db, "attempt", attempt.id, dst)
            else:
                with pytest.raises(InvalidTransition):
                    await transition(db, "attempt", attempt.id, dst)


async def _force_status(db, table: str, row_id: str, status: str) -> None:  # type: ignore[no-untyped-def]
    """Test-only direct status write to explore every source state (bypasses FSM)."""
    await db.execute(f"UPDATE {table} SET status = ? WHERE id = ?", (status, row_id))
    await db.conn.commit()


# ------------------------------------------------------------------- audit trail


async def test_transition_writes_audit_event(db) -> None:  # type: ignore[no-untyped-def]
    _, run, _, _, _ = await _seed(db)
    await transition_run(db, run.id, RunStatus.SPEC_PENDING, reason="spec dispatched")
    rows = await db.fetchall(
        "SELECT * FROM agent_events WHERE event_type = 'state_transition' ORDER BY id"
    )
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload == {
        "entity": "run",
        "id": run.id,
        "from": "draft",
        "to": "spec_pending",
        "reason": "spec dispatched",
    }
    assert rows[0]["run_id"] == run.id


async def test_task_and_attempt_events_carry_context(db) -> None:  # type: ignore[no-untyped-def]
    _, run, _, task, attempt = await _seed(db)
    await transition_task(db, task.id, TaskStatus.SCHEDULED)
    await transition_attempt(db, attempt.id, AttemptStatus.RUNNING)
    task_event = await db.fetchone(
        "SELECT * FROM agent_events WHERE run_id = ? AND event_type='state_transition'",
        (run.id,),
    )
    assert task_event is not None
    att_event = await db.fetchone(
        "SELECT * FROM agent_events WHERE attempt_id = ? AND event_type='state_transition'",
        (attempt.id,),
    )
    assert att_event is not None and json.loads(att_event["payload_json"])["entity"] == "attempt"
    assert att_event["run_id"] == run.id


# ----------------------------------------------------------------- edge tables


async def test_edge_tables_match_implementation_plan(db) -> None:  # type: ignore[no-untyped-def]
    """Pin the adjacency tables to impl-plan §5.1-5.3, as (from, to) string pairs.

    Non-literal-table additions (e.g. spec_pending→failed = spec-generation
    failure) are documented deviations from the §5 prose, not test typos.
    """
    from girder.fsm import _ATTEMPT_EDGES, _RUN_EDGES, _TASK_EDGES

    def pairs(table):  # type: ignore[no-untyped-def]
        return {(f.value, t.value) for f, targets in table.items() for t in targets}

    expected_runs = {
        ("draft", "spec_pending"),
        ("draft", "aborted"),
        ("spec_pending", "spec_pending"),
        ("spec_pending", "spec_approved"),
        ("spec_pending", "failed"),
        ("spec_pending", "aborted"),
        ("spec_pending", "budget_exhausted"),
        ("spec_approved", "baseline_running"),
        ("spec_approved", "aborted"),
        ("baseline_running", "active"),
        ("baseline_running", "escalated"),
        ("baseline_running", "failed"),
        ("baseline_running", "aborted"),
        ("active", "awaiting_amendment"),
        ("active", "pr_open"),
        ("active", "failed"),
        ("active", "aborted"),
        ("active", "budget_exhausted"),
        ("active", "escalated"),  # Phase 4: unresolvable wave conflict escalates
        ("awaiting_amendment", "active"),
        ("awaiting_amendment", "pr_open"),
        ("awaiting_amendment", "aborted"),
        ("awaiting_amendment", "failed"),  # §5.1: "active / any → failed"
        ("awaiting_amendment", "budget_exhausted"),
        ("pr_open", "ci_running"),
        ("pr_open", "aborted"),
        ("pr_open", "escalated"),
        ("pr_open", "budget_exhausted"),
        ("ci_running", "ci_fixing"),
        ("ci_running", "conformance_review"),
        ("ci_running", "failed"),
        ("ci_running", "aborted"),
        ("ci_running", "escalated"),
        ("ci_running", "budget_exhausted"),
        ("ci_fixing", "ci_running"),
        ("ci_fixing", "failed"),
        ("ci_fixing", "aborted"),
        ("ci_fixing", "escalated"),
        ("ci_fixing", "budget_exhausted"),
        ("conformance_review", "merge_pending_human"),
        ("conformance_review", "merged"),
        ("conformance_review", "escalated"),
        ("conformance_review", "aborted"),
        ("conformance_review", "budget_exhausted"),
        ("merge_pending_human", "merged"),
        ("merge_pending_human", "aborted"),
        ("merge_pending_human", "escalated"),
        ("budget_exhausted", "aborted"),
        # §8.3: ceilings are per-cap, not per-run-lifetime — an operator
        # raising budget_cap_usd resumes the run (WP-E defect 2).
        ("budget_exhausted", "active"),
        # §5.1: escalated is a hard stop (D4 — human review at every tier);
        # only an explicit operator abort leaves it.
        ("escalated", "aborted"),
    }
    expected_tasks = {
        ("pending", "scheduled"),
        ("pending", "skipped"),
        ("pending", "force_passed"),
        ("scheduled", "pending"),
        ("scheduled", "running"),
        ("scheduled", "skipped"),
        ("scheduled", "dropped"),
        ("scheduled", "force_passed"),
        ("running", "verifying"),
        ("running", "awaiting_amendment"),
        ("running", "retry_scheduled"),
        ("running", "failed"),
        ("running", "dropped"),
        ("running", "skipped"),
        ("running", "force_passed"),
        ("verifying", "verify_passed"),
        ("verifying", "retry_scheduled"),
        ("verifying", "failed"),
        ("verifying", "dropped"),
        ("verifying", "skipped"),
        ("verifying", "force_passed"),
        ("verify_passed", "completed"),
        ("verify_passed", "retry_scheduled"),
        ("verify_passed", "dropped"),  # Phase 4: verified-green wave task can be dropped
        ("retry_scheduled", "running"),
        ("retry_scheduled", "failed"),
        ("retry_scheduled", "dropped"),
        ("retry_scheduled", "skipped"),
        ("retry_scheduled", "force_passed"),
        ("awaiting_amendment", "running"),
        ("awaiting_amendment", "dropped"),
        ("awaiting_amendment", "skipped"),
        ("awaiting_amendment", "force_passed"),
    }
    expected_attempts = {
        ("initialized", "running"),
        ("running", "succeeded"),
        ("running", "failed"),
        ("running", "timeout"),
        ("running", "crashed"),
        ("running", "budget_frozen"),
        ("running", "amendment_requested"),
        ("running", "integrity_violation"),
    }

    assert pairs(_RUN_EDGES) == expected_runs
    assert pairs(_TASK_EDGES) == expected_tasks
    assert pairs(_ATTEMPT_EDGES) == expected_attempts


# ------------------------------------------------------------------ concurrency


async def test_concurrent_transitions_from_same_state_exactly_one_wins(db) -> None:  # type: ignore[no-untyped-def]
    """A3 regression: the check-and-set must be atomic under BEGIN IMMEDIATE."""
    import asyncio

    from girder.fsm import InvalidTransition, transition_run

    _, run, _, _, _ = await _seed(db)
    # Two diverging edges from spec_pending (neither chains into the other).
    await transition_run(db, run.id, RunStatus.SPEC_PENDING)
    results = await asyncio.gather(
        transition_run(db, run.id, RunStatus.SPEC_APPROVED),
        transition_run(db, run.id, RunStatus.FAILED),
        return_exceptions=True,
    )
    strs = [r for r in results if isinstance(r, str)]
    invalid = [r for r in results if isinstance(r, InvalidTransition)]
    assert len(strs) == 1
    assert len(invalid) == 1
    assert all(isinstance(r, BaseException) for r in results if not isinstance(r, str))

    fresh = await repo.get_run(db, run.id)
    assert fresh is not None and fresh.status.value == strs[0]
    rows = await db.fetchall(
        "SELECT payload_json FROM agent_events WHERE event_type='state_transition' AND run_id = ?",
        (run.id,),
    )
    # the run's seed transition (draft -> spec_pending) also logged an event;
    # only one event may originate from the contended source state.
    events = [r for r in rows if json.loads(r["payload_json"])["from"] == "spec_pending"]
    assert len(events) == 1
    assert json.loads(events[0]["payload_json"])["to"] == strs[0]


# --------------------------------------------------------------------- atomicity


async def test_transition_is_atomic_crash_between_statements(db, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """If the audit-event insert fails, the status update must NOT survive."""
    _, run, _, _, _ = await _seed(db)

    original_execute = db.conn.execute

    def failing_execute(sql: str, params: tuple = ()):  # type: ignore[no-untyped-def]
        # aiosqlite's execute() is a *sync* call returning a dual-protocol Cursor
        # (awaitable AND an async context manager) — mirror that shape exactly.
        if "agent_events" in sql:
            raise RuntimeError("simulated crash between UPDATE and INSERT")
        return original_execute(sql, params)

    monkeypatch.setattr(db.conn, "execute", failing_execute)
    with pytest.raises(RuntimeError, match="simulated crash"):
        await transition_run(db, run.id, RunStatus.SPEC_PENDING)
    monkeypatch.setattr(db.conn, "execute", original_execute)

    fresh = await repo.get_run(db, run.id)
    assert fresh is not None and fresh.status == RunStatus.DRAFT  # neither landed
    events = await db.fetchall("SELECT * FROM agent_events WHERE event_type='state_transition'")
    assert len(events) == 0


async def test_spec_pending_may_go_budget_exhausted(db) -> None:  # type: ignore[no-untyped-def]
    """Spec generation is spend-bearing (plan.md §5.1 'any spend-bearing')."""
    _, run, _, _, _ = await _seed(db)
    await _force_status(db, "runs", run.id, RunStatus.SPEC_PENDING.value)
    new = await transition_run(db, run.id, RunStatus.BUDGET_EXHAUSTED)
    assert new == "budget_exhausted"


async def test_draft_cannot_go_budget_exhausted(db) -> None:  # type: ignore[no-untyped-def]
    _, run, _, _, _ = await _seed(db)
    with pytest.raises(InvalidTransition, match="draft -> budget_exhausted"):
        await transition_run(db, run.id, RunStatus.BUDGET_EXHAUSTED)


# ------------------------------------------------------------------- self-loops


async def test_spec_pending_self_loop_is_declared_and_audited(db) -> None:  # type: ignore[no-untyped-def]
    """§5.1: 'Regenerate' self-loop — a new draft replaces the old, same state."""
    _, run, _, _, _ = await _seed(db)
    await transition_run(db, run.id, RunStatus.SPEC_PENDING, reason="spec dispatched")

    result = await transition_run(db, run.id, RunStatus.SPEC_PENDING, reason="regenerate")
    assert result == RunStatus.SPEC_PENDING.value
    assert await current_status(db, "run", run.id) == RunStatus.SPEC_PENDING.value

    rows = await db.fetchall(
        "SELECT payload_json FROM agent_events"
        " WHERE event_type = 'state_transition' AND run_id = ? ORDER BY id",
        (run.id,),
    )
    assert json.loads(rows[-1]["payload_json"]) == {
        "entity": "run",
        "id": run.id,
        "from": "spec_pending",
        "to": "spec_pending",
        "reason": "regenerate",
    }


async def test_undeclared_self_transition_rejected(db) -> None:  # type: ignore[no-untyped-def]
    _, run, _, task, attempt = await _seed(db)
    for state in (
        RunStatus.SPEC_PENDING,
        RunStatus.SPEC_APPROVED,
        RunStatus.BASELINE_RUNNING,
        RunStatus.ACTIVE,
    ):
        await transition_run(db, run.id, state)
    with pytest.raises(InvalidTransition, match="active -> active"):
        await transition_run(db, run.id, RunStatus.ACTIVE)
    await transition_task(db, task.id, TaskStatus.SCHEDULED)
    await transition_task(db, task.id, TaskStatus.RUNNING)
    with pytest.raises(InvalidTransition, match="running -> running"):
        await transition_task(db, task.id, TaskStatus.RUNNING)
    await transition_attempt(db, attempt.id, AttemptStatus.RUNNING)
    with pytest.raises(InvalidTransition):
        await transition_attempt(db, attempt.id, AttemptStatus.RUNNING)


# ------------------------------------------------------------------ self-test (§6.3)


async def test_self_test_passes_and_needs_no_fixture(db) -> None:  # type: ignore[no-untyped-def]
    """Boot-time self-test: walks its own temp DB, not the fixture's."""
    await fsm.self_test()  # must not raise


async def test_self_test_fails_when_tables_and_engine_disagree(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    """Sabotage the engine (accept every transition): the self-test must
    catch illegal edges that no longer raise."""
    from girder import fsm as fsm_module

    async def permissive(db, kind, row_id, new_state, payload=None):  # type: ignore[no-untyped-def]
        return str(new_state)

    monkeypatch.setattr(fsm_module, "transition", permissive)
    with pytest.raises(fsm.FsmSelfTestError, match="run draft->spec_approved"):
        await fsm.self_test()


async def test_awaiting_amendment_may_go_failed(db) -> None:  # type: ignore[no-untyped-def]
    """§5.1: task exhausts task_max_attempts while the run awaits amendment."""
    _, run, _, _, _ = await _seed(db)
    await transition_run(db, run.id, RunStatus.SPEC_PENDING)
    await _force_status(db, "runs", run.id, RunStatus.AWAITING_AMENDMENT.value)
    assert await transition_run(db, run.id, RunStatus.FAILED) == "failed"


async def test_escalated_is_a_hard_stop(db) -> None:  # type: ignore[no-untyped-def]
    """§5.1 / D4: no automated exit from escalated — only operator abort."""
    _, run, _, _, _ = await _seed(db)
    await transition_run(db, run.id, RunStatus.SPEC_PENDING)
    await _force_status(db, "runs", run.id, RunStatus.ESCALATED.value)
    with pytest.raises(InvalidTransition, match="escalated -> merged"):
        await transition_run(db, run.id, RunStatus.MERGED)
    with pytest.raises(InvalidTransition, match="escalated -> active"):
        await transition_run(db, run.id, RunStatus.ACTIVE)
    # explicit operator abort remains possible
    assert await transition_run(db, run.id, RunStatus.ABORTED) == "aborted"
