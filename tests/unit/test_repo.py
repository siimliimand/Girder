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
