"""Integration assertion for WP 8.4: the index block reaches attempt start.

Reuses the task_engine harness (real temp git repo, scripted suite results,
no podman). Per ws03-codebase-index.md: with ``inject_index = true`` (the
default) the ``[TRUSTED] Codebase index`` block is in the initial context
and the JSON blob persists to ``tasks.codebase_index_json``; with it
disabled, neither happens.
"""

from __future__ import annotations

import pytest

from girder.config import LimitsConfig
from girder.db import repo
from girder.db.models import TaskStatus
from girder.orchestrator.task_engine import TaskEngine, TaskOutcome

# Re-exported fixtures/helpers from the shared task-engine harness.
from tests.unit.test_task_engine import (  # noqa: F401
    FakeGateway,
    Harness,
    _seed_task,
    harness,
    write_commit_complete,
)

pytestmark = pytest.mark.integration


def _first_user_content(gateway: FakeGateway) -> str:
    msgs = gateway.calls[0]
    msg = next(m for m in msgs if m.role == "user")
    return str(msg.content)


async def test_attempt_start_injects_index_block(harness: Harness) -> None:  # noqa: F811
    h = harness
    task = await _seed_task(h, scope_globs=["src/**"])
    gw = FakeGateway(responses=write_commit_complete())
    engine: TaskEngine = h.engine(gw)
    outcome: TaskOutcome = await engine.execute_task(h.run, task)
    assert outcome.kind == "completed"

    # The block opened the attempt's initial context (first gateway call).
    content = _first_user_content(gw)
    assert "[TRUSTED] Codebase index (relevant to your scope):" in content
    assert "lib.py" in content  # in-scope src/ file from the seeded repo
    assert "test_p.py" not in content  # out-of-scope tests/ file is sliced away

    # JSON blob persisted to tasks.codebase_index_json (migration 014).
    fresh = await repo.get_task(h.db, task.id)
    assert fresh is not None and fresh.codebase_index_json is not None
    assert "src/lib.py" in fresh.codebase_index_json


async def test_attempt_start_inject_disabled(harness: Harness) -> None:  # noqa: F811
    h = harness
    h.settings.limits = LimitsConfig(
        task_max_attempts=2,
        attempt_max_turns=6,
        attempt_wallclock_s=60,
        inject_index=False,
        planning_turns=0,
    )
    task = await _seed_task(h)
    gw = FakeGateway(responses=write_commit_complete())
    engine: TaskEngine = h.engine(gw)
    outcome: TaskOutcome = await engine.execute_task(h.run, task)
    assert outcome.kind == "completed"
    assert "[TRUSTED] Codebase index" not in _first_user_content(gw)
    fresh = await repo.get_task(h.db, task.id)
    assert fresh is not None and fresh.status is TaskStatus.COMPLETED
    assert fresh.codebase_index_json is None
