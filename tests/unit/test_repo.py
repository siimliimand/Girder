"""WP-1.x — repo write guards: status columns are FSM-only, whitelists hold."""

from __future__ import annotations

import pytest

from girder.db import repo
from girder.db.models import TaskType

_STATUS_MSG = "girder.fsm.transition"


async def test_update_run_fields_rejects_status(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    with pytest.raises(ValueError, match=_STATUS_MSG):
        await repo.update_run_fields(db, run.id, status="active")


async def test_update_task_fields_rejects_status(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    task = await repo.create_task(db, wave.id, 1, "t", TaskType.CODE_CHANGE)
    with pytest.raises(ValueError, match=_STATUS_MSG):
        await repo.update_task_fields(db, task.id, status="running")


async def test_update_attempt_fields_rejects_status(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    task = await repo.create_task(db, wave.id, 1, "t", TaskType.CODE_CHANGE)
    attempt = await repo.create_attempt(db, task.id, "abc123")
    with pytest.raises(ValueError, match=_STATUS_MSG):
        await repo.update_attempt_fields(db, attempt.id, status="running")


async def test_update_run_fields_rejects_unknown_column(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    with pytest.raises(ValueError, match="nonsense_column"):
        await repo.update_run_fields(db, run.id, nonsense_column=1)


async def test_update_task_fields_rejects_unknown_column(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    task = await repo.create_task(db, wave.id, 1, "t", TaskType.CODE_CHANGE)
    with pytest.raises(ValueError, match="nonsense_column"):
        await repo.update_task_fields(db, task.id, nonsense_column=1)


async def test_update_attempt_fields_rejects_unknown_column(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    task = await repo.create_task(db, wave.id, 1, "t", TaskType.CODE_CHANGE)
    attempt = await repo.create_attempt(db, task.id, "abc123")
    with pytest.raises(ValueError, match="nonsense_column"):
        await repo.update_attempt_fields(db, attempt.id, nonsense_column=1)


async def test_whitelisted_run_field_persists(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    await repo.update_run_fields(db, run.id, spec_hash="abc", pr_number=42)
    fresh = await repo.get_run(db, run.id)
    assert fresh is not None
    assert fresh.spec_hash == "abc"
    assert fresh.pr_number == 42


async def test_token_usage_roundtrip_with_attempt(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    task = await repo.create_task(db, wave.id, 1, "t", TaskType.CODE_CHANGE)
    attempt = await repo.create_attempt(db, task.id, "abc123")

    usage_id = await repo.insert_token_usage(
        db,
        run_id=run.id,
        attempt_id=attempt.id,
        model_role="tier1",
        model_id="m1",
        estimated_before_call=0.01,
    )
    rows = await repo.list_token_usage_for_run(db, run.id)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == usage_id
    assert row["attempt_id"] == attempt.id
    assert row["model_role"] == "tier1"
    assert row["model_id"] == "m1"
    assert row["estimated_before_call"] == pytest.approx(0.01)
    assert row["prompt_tokens"] == 0
    assert row["completion_tokens"] == 0
    assert row["cost_usd"] == 0.0

    await repo.update_token_usage_actual(
        db, usage_id, prompt_tokens=120, completion_tokens=45, cost_usd=0.003
    )
    rows = await repo.list_token_usage_for_run(db, run.id)
    assert rows[0]["prompt_tokens"] == 120
    assert rows[0]["completion_tokens"] == 45
    assert rows[0]["cost_usd"] == pytest.approx(0.003)


async def test_token_usage_run_context_without_attempt(db) -> None:  # type: ignore[no-untyped-def]
    """Spec generation spends before any attempt exists (plan.md Phase 1)."""
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    usage_id = await repo.insert_token_usage(
        db,
        run_id=run.id,
        attempt_id=None,
        model_role="tier1",
        model_id="m1",
        estimated_before_call=0.02,
    )
    rows = await repo.list_token_usage_for_run(db, run.id)
    assert [r["id"] for r in rows] == [usage_id]
    assert rows[0]["run_id"] == run.id
    assert rows[0]["attempt_id"] is None


async def test_token_usage_enforces_foreign_keys(db) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(Exception, match="FOREIGN KEY"):
        await repo.insert_token_usage(
            db,
            run_id="no-such-run",
            attempt_id=None,
            model_role="tier1",
            model_id="m1",
            estimated_before_call=0.0,
        )


async def test_update_run_fields_accepts_proposal_md_and_branch(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    await repo.update_run_fields(
        db, run.id, proposal_md="# Proposal", branch="girder/run-abc1"
    )
    fresh = await repo.get_run(db, run.id)
    assert fresh is not None
    assert fresh.proposal_md == "# Proposal"
    assert fresh.branch == "girder/run-abc1"


# ------------------------------------------------------- sprint 3 aggregates


async def test_baseline_run_roundtrip(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    baseline = await repo.create_baseline_run(db, project.id, "abc123", '{"t.py::test_x": "pass"}')
    fresh = await repo.get_baseline_run(db, baseline.id)
    assert fresh is not None
    assert fresh.commit_sha == "abc123"
    assert fresh.project_id == project.id
    assert fresh.per_test_json == '{"t.py::test_x": "pass"}'
    assert fresh.created_at
    assert await repo.get_baseline_run(db, "nope") is None


async def test_upsert_flaky_test_preserves_first_seen_run(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run1 = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    run2 = await repo.create_run(db, project.id, "intent", "run/abc2", 5.0)

    await repo.upsert_flaky_test(db, project.id, "t.py::test_flaky", run_id=run1.id)
    await repo.upsert_flaky_test(db, project.id, "t.py::test_flaky", run_id=run2.id)

    tests = await repo.list_flaky_tests(db, project.id)
    assert len(tests) == 1
    ft = tests[0]
    assert ft.first_seen_run == run1.id
    assert ft.last_seen_run == run2.id
    assert ft.status == "known_flaky"
    assert ft.test_id == "t.py::test_flaky"


async def test_upsert_flaky_test_updates_status(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    await repo.upsert_flaky_test(db, project.id, "t.py::test_x", run_id=run.id)
    await repo.upsert_flaky_test(
        db, project.id, "t.py::test_x", run_id=run.id, status="chronic"
    )
    tests = await repo.list_flaky_tests(db, project.id)
    assert [ft.status for ft in tests] == ["chronic"]


async def test_spec_amendment_roundtrip_and_listing(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    task = await repo.create_task(db, wave.id, 1, "t", TaskType.CODE_CHANGE)

    a = await repo.create_spec_amendment(db, run.id, "spec misses retry path", "+ retry: on")
    b = await repo.create_spec_amendment(
        db, run.id, "spec misses cleanup", "- tmp", task_id=task.id
    )
    assert a.status == "pending"
    assert a.task_id is None
    assert b.task_id == task.id

    assert await repo.get_spec_amendment(db, a.id) == a
    assert await repo.get_spec_amendment(db, "nope") is None

    listed = await repo.list_amendments_for_run(db, run.id)
    assert [x.id for x in listed] == [a.id, b.id]

    pending = await repo.get_pending_amendment(db, run.id)
    assert pending is not None
    assert pending.id == b.id  # newest pending wins

    await repo.resolve_spec_amendment(
        db, b.id, status="approved", guidance="ok", new_spec_hash="deadbeef"
    )
    resolved = await repo.get_spec_amendment(db, b.id)
    assert resolved is not None
    assert resolved.status == "approved"
    assert resolved.guidance == "ok"
    assert resolved.new_spec_hash == "deadbeef"
    assert resolved.resolved_at is not None

    still_pending = await repo.get_pending_amendment(db, run.id)
    assert still_pending is not None
    assert still_pending.id == a.id


async def test_resolve_spec_amendment_rejects_bad_status(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    amendment = await repo.create_spec_amendment(db, run.id, "why", "+ change")
    with pytest.raises(ValueError, match="approved"):
        await repo.resolve_spec_amendment(db, amendment.id, status="pending")
    with pytest.raises(ValueError, match="nonsense"):
        await repo.resolve_spec_amendment(db, amendment.id, status="nonsense")
    untouched = await repo.get_spec_amendment(db, amendment.id)
    assert untouched is not None
    assert untouched.status == "pending"


async def test_tool_call_roundtrip(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    task = await repo.create_task(db, wave.id, 1, "t", TaskType.CODE_CHANGE)
    attempt = await repo.create_attempt(db, task.id, "abc123")

    call_id = await repo.insert_tool_call(
        db, attempt_id=attempt.id, tool_name="write_file", input_json='{"path": "a.py"}'
    )
    other = await repo.insert_tool_call(
        db,
        attempt_id=attempt.id,
        tool_name="shell",
        input_json='{"cmd": "ls"}',
        output_redacted="file1\nfile2",
        duration_ms=120,
        scope_violation=True,
        held=True,
    )
    rows = await repo.list_tool_calls_for_attempt(db, attempt.id)
    assert [r["id"] for r in rows] == [call_id, other]
    held = rows[1]
    assert held["tool_name"] == "shell"
    assert held["output_blob_redacted"] == "file1\nfile2"
    assert held["duration_ms"] == 120
    assert held["scope_violation"] == 1
    assert held["held"] == 1
    assert rows[0]["scope_violation"] == 0
    assert rows[0]["held"] == 0
    assert await repo.list_tool_calls_for_attempt(db, attempt.id) == rows
    assert await repo.list_tool_calls_for_attempt(db, "nope") == []


async def test_consume_steering_events_returns_then_marks(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)

    await repo.insert_steering_event(db, run.id, "pause", {"reason": "human"})
    await repo.insert_steering_event(db, run.id, "inject", {"text": "use ruff"})

    events = await repo.consume_steering_events(db, run.id)
    assert [e["kind"] for e in events] == ["pause", "inject"]  # oldest first
    assert events[0]["payload"] == {"reason": "human"}
    assert events[1]["payload"] == {"text": "use ruff"}
    assert all(e["run_id"] == run.id for e in events)

    # Second consume drains nothing — exactly-once delivery.
    assert await repo.consume_steering_events(db, run.id) == []


async def test_consume_steering_events_kind_filter(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    other = await repo.create_run(db, project.id, "intent", "run/abc2", 5.0)

    await repo.insert_steering_event(db, run.id, "pause", {})
    await repo.insert_steering_event(db, run.id, "skip", {"task_id": "t1"})
    await repo.insert_steering_event(db, other.id, "skip", {"task_id": "other-run"})

    events = await repo.consume_steering_events(db, run.id, kinds=["skip"])
    assert [e["kind"] for e in events] == ["skip"]
    assert events[0]["payload"] == {"task_id": "t1"}

    # pause is untouched by the filtered consume; other run's event too.
    remaining = await repo.consume_steering_events(db, run.id)
    assert [e["kind"] for e in remaining] == ["pause"]
    assert [e["kind"] for e in await repo.consume_steering_events(db, other.id)] == ["skip"]


# ---------------------------------------------------------- sprint 5: waves


async def test_get_or_create_wave_is_idempotent(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave = await repo.get_or_create_wave(db, run.id, 0)
    again = await repo.get_or_create_wave(db, run.id, 0)
    assert again.id == wave.id
    assert again.sequence_order == 0
    assert again.run_id == run.id


async def test_get_or_create_wave_distinct_sequence_orders(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    w0 = await repo.get_or_create_wave(db, run.id, 0)
    w1 = await repo.get_or_create_wave(db, run.id, 1)
    assert w0.id != w1.id
    waves = await repo.list_waves_for_run(db, run.id)
    assert [w.sequence_order for w in waves] == [0, 1]


async def test_list_waves_for_run_orders_by_sequence(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    other = await repo.create_run(db, project.id, "intent", "run/abc2", 5.0)
    await repo.get_or_create_wave(db, run.id, 2)
    await repo.get_or_create_wave(db, run.id, 0)
    await repo.get_or_create_wave(db, run.id, 1)
    await repo.get_or_create_wave(db, other.id, 0)

    waves = await repo.list_waves_for_run(db, run.id)
    assert [w.sequence_order for w in waves] == [0, 1, 2]
    assert all(w.run_id == run.id for w in waves)


async def test_set_wave_status(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave = await repo.get_or_create_wave(db, run.id, 0)
    assert wave.status == "pending"
    await repo.set_wave_status(db, wave.id, "running")
    waves = await repo.list_waves_for_run(db, run.id)
    assert waves[0].status == "running"


async def test_list_tasks_for_wave_orders_and_isolates(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave_a = await repo.get_or_create_wave(db, run.id, 0)
    wave_b = await repo.get_or_create_wave(db, run.id, 1)
    await repo.create_task(db, wave_a.id, 2, "b", TaskType.CODE_CHANGE)
    await repo.create_task(db, wave_a.id, 1, "a", TaskType.CODE_CHANGE)
    await repo.create_task(db, wave_b.id, 1, "other-wave", TaskType.TEST_CHANGE)

    tasks_a = await repo.list_tasks_for_wave(db, wave_a.id)
    assert [t.seq for t in tasks_a] == [1, 2]
    assert [t.title for t in tasks_a] == ["a", "b"]
    assert all(t.wave_id == wave_a.id for t in tasks_a)

    tasks_b = await repo.list_tasks_for_wave(db, wave_b.id)
    assert [t.title for t in tasks_b] == ["other-wave"]


async def test_list_attempts_for_task_orders_by_attempt_num(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    task = await repo.create_task(db, wave.id, 1, "t", TaskType.CODE_CHANGE)
    other = await repo.create_task(db, wave.id, 2, "u", TaskType.CODE_CHANGE)
    a1 = await repo.create_attempt(db, task.id, "abc123")
    a2 = await repo.create_attempt(db, task.id, "abc123")
    await repo.create_attempt(db, other.id, "abc123")

    attempts = await repo.list_attempts_for_task(db, task.id)
    assert [a.attempt_num for a in attempts] == [1, 2]
    assert [a.id for a in attempts] == [a1.id, a2.id]
    assert all(a.task_id == task.id for a in attempts)


async def test_update_task_fields_wave_id_moves_task(db) -> None:  # type: ignore[no-untyped-def]
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave0 = await repo.get_or_create_wave(db, run.id, 0)
    wave1 = await repo.get_or_create_wave(db, run.id, 1)
    task = await repo.create_task(db, wave0.id, 1, "t", TaskType.CODE_CHANGE)

    await repo.update_task_fields(db, task.id, wave_id=wave1.id)
    fresh = await repo.get_task(db, task.id)
    assert fresh is not None
    assert fresh.wave_id == wave1.id
    assert [t.id for t in await repo.list_tasks_for_wave(db, wave1.id)] == [task.id]
    assert await repo.list_tasks_for_wave(db, wave0.id) == []


async def test_tool_call_verdict_roundtrip(db) -> None:  # type: ignore[no-untyped-def]
    """Migration 011: ScopeGuard verdict per tool call is persisted and read
    back by the post-mortem listing."""
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    task = await repo.create_task(db, wave.id, 1, "t", TaskType.CODE_CHANGE)
    attempt = await repo.create_attempt(db, task.id, "abc123")

    await repo.insert_tool_call(
        db,
        attempt_id=attempt.id,
        tool_name="write_file",
        input_json="{}",
        verdict="violation",
    )
    await repo.insert_tool_call(db, attempt_id=attempt.id, tool_name="shell", input_json="{}")
    await repo.insert_tool_call(
        db, attempt_id=attempt.id, tool_name="shell", input_json="{}", verdict="allow"
    )

    rows = await repo.list_tool_calls_for_attempt(db, attempt.id)
    assert [r["verdict"] for r in rows] == ["violation", None, "allow"]


async def test_attempt_prompts_roundtrip(db) -> None:  # type: ignore[no-untyped-def]
    """Migration 011 / plan.md Phase 5 task 5: per-turn prompt snapshots,
    ordered by (turn, id), redaction owned by the caller."""
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    task = await repo.create_task(db, wave.id, 1, "t", TaskType.CODE_CHANGE)
    attempt = await repo.create_attempt(db, task.id, "abc123")

    await repo.record_attempt_prompt(db, attempt.id, 1, "user", "fix the bug", run_id=run.id)
    await repo.record_attempt_prompt(db, attempt.id, 1, "assistant", "on it")
    await repo.record_attempt_prompt(db, attempt.id, 2, "user", "sk: REDACTED")

    prompts = await repo.list_attempt_prompts(db, attempt.id)
    assert [(p.turn, p.role, p.content_redacted) for p in prompts] == [
        (1, "user", "fix the bug"),
        (1, "assistant", "on it"),
        (2, "user", "sk: REDACTED"),
    ]
    assert prompts[0].run_id == run.id
    assert prompts[1].run_id is None
    assert all(p.ts for p in prompts)
    assert await repo.list_attempt_prompts(db, "nope") == []


async def test_attempt_diffs_roundtrip(db) -> None:  # type: ignore[no-untyped-def]
    """Migration 012 / §10 diff viewer: per-attempt diff rows, redaction owned
    by the caller, latest-per-task lookup."""
    project = await repo.create_project(db, "p", "/repo")
    run = await repo.create_run(db, project.id, "intent", "run/abc1", 5.0)
    wave = await repo.get_or_create_wave0(db, run.id)
    task = await repo.create_task(db, wave.id, 1, "t", TaskType.CODE_CHANGE)
    attempt = await repo.create_attempt(db, task.id, "abc123")

    assert await repo.list_attempt_diffs_for_run(db, run.id) == []
    assert await repo.latest_attempt_diff_for_task(db, task.id) is None

    await repo.insert_attempt_diff(
        db,
        attempt_id=attempt.id,
        task_id=task.id,
        run_id=run.id,
        base_commit="abc123",
        head_commit="def456",
        diff_redacted="diff --git a/x b/x",
    )
    await repo.insert_attempt_diff(
        db,
        attempt_id=attempt.id,
        task_id=task.id,
        run_id=run.id,
        base_commit="def456",
        head_commit="789abc",
        diff_redacted="diff --git a/y b/y",
    )

    diffs = await repo.list_attempt_diffs_for_run(db, run.id)
    assert [d.diff_redacted for d in diffs] == [
        "diff --git a/x b/x",
        "diff --git a/y b/y",
    ]
    assert all(d.run_id == run.id and d.task_id == task.id for d in diffs)
    assert all(d.created_at for d in diffs)
    latest = await repo.latest_attempt_diff_for_task(db, task.id)
    assert latest is not None and latest.diff_redacted == "diff --git a/y b/y"
    assert await repo.list_attempt_diffs_for_run(db, "nope") == []
    assert await repo.latest_attempt_diff_for_task(db, "nope") is None
