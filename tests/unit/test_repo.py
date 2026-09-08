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
