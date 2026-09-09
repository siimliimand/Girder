"""Guarded state-transition engine — the single mutation point for statuses.

Every status change in the system funnels through :func:`transition`:

* the edge must exist in the adjacency tables below (mirrors impl-plan §5),
* the row update **and** its ``agent_events`` audit row land in one
  ``BEGIN IMMEDIATE`` transaction — crash between statements means *neither*
  lands (§5.5: state is persisted before external action).

Illegal edges raise :class:`InvalidTransition` rather than being tolerated.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from girder.db import repo
from girder.db.engine import Database, default_migrations_dir
from girder.db.models import AttemptStatus, RunStatus, TaskStatus, TaskType
from girder.util import utcnow_iso

_TABLE = {"run": "runs", "task": "tasks", "attempt": "attempts"}

# ------------------------------------------------------------------- edge tables

# Run states (impl-plan §5.1). Terminal: merged / failed / aborted.
_RUN_EDGES: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.DRAFT: frozenset({RunStatus.SPEC_PENDING, RunStatus.ABORTED}),
    # spec_pending is spend-bearing (generation calls the model), so the
    # BudgetGuard tripwire routes it to budget_exhausted (plan.md §5.1);
    # Sprint 1 omitted the edge because nothing spent money yet.
    RunStatus.SPEC_PENDING: frozenset(
        {
            RunStatus.SPEC_PENDING,
            RunStatus.SPEC_APPROVED,
            RunStatus.FAILED,
            RunStatus.ABORTED,
            RunStatus.BUDGET_EXHAUSTED,
        }
    ),
    RunStatus.SPEC_APPROVED: frozenset({RunStatus.BASELINE_RUNNING, RunStatus.ABORTED}),
    RunStatus.BASELINE_RUNNING: frozenset(
        {RunStatus.ACTIVE, RunStatus.ESCALATED, RunStatus.FAILED, RunStatus.ABORTED}
    ),
    RunStatus.ACTIVE: frozenset(
        {
            RunStatus.AWAITING_AMENDMENT,
            RunStatus.PR_OPEN,
            RunStatus.FAILED,
            RunStatus.ABORTED,
            RunStatus.BUDGET_EXHAUSTED,
            # Phase 4: an unresolvable wave-integration conflict drops the
            # task and escalates to the user regardless of autonomy tier.
            RunStatus.ESCALATED,
        }
    ),
    # §5.1: "active / any → failed" — a task exhausting task_max_attempts
    # fails the run even while the run sits awaiting a user amendment.
    RunStatus.AWAITING_AMENDMENT: frozenset(
        {
            RunStatus.ACTIVE,
            RunStatus.PR_OPEN,
            RunStatus.FAILED,
            RunStatus.ABORTED,
            RunStatus.BUDGET_EXHAUSTED,
        }
    ),
    RunStatus.PR_OPEN: frozenset(
        {RunStatus.CI_RUNNING, RunStatus.ABORTED, RunStatus.ESCALATED, RunStatus.BUDGET_EXHAUSTED}
    ),
    RunStatus.CI_RUNNING: frozenset(
        {
            RunStatus.CI_FIXING,
            RunStatus.CONFORMANCE_REVIEW,
            RunStatus.FAILED,
            RunStatus.ABORTED,
            RunStatus.ESCALATED,
            RunStatus.BUDGET_EXHAUSTED,
        }
    ),
    RunStatus.CI_FIXING: frozenset(
        {
            RunStatus.CI_RUNNING,
            RunStatus.FAILED,
            RunStatus.ABORTED,
            RunStatus.ESCALATED,
            RunStatus.BUDGET_EXHAUSTED,
        }
    ),
    RunStatus.CONFORMANCE_REVIEW: frozenset(
        {
            RunStatus.MERGE_PENDING_HUMAN,
            RunStatus.MERGED,
            RunStatus.ESCALATED,
            RunStatus.ABORTED,
            RunStatus.BUDGET_EXHAUSTED,
        }
    ),
    RunStatus.MERGE_PENDING_HUMAN: frozenset(
        {RunStatus.MERGED, RunStatus.ABORTED, RunStatus.ESCALATED}
    ),
    RunStatus.MERGED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.ABORTED: frozenset(),
    RunStatus.BUDGET_EXHAUSTED: frozenset({RunStatus.ABORTED}),
    # §5.1: escalated is a hard stop — a human reviews at every tier (D4).
    # No automated exit exists: only an explicit operator abort leaves it.
    RunStatus.ESCALATED: frozenset({RunStatus.ABORTED}),
}

# Task states (impl-plan §5.2). Terminal: completed / failed / skipped /
# force_passed / dropped.
_TASK_EDGES: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.SCHEDULED, TaskStatus.SKIPPED, TaskStatus.FORCE_PASSED}
    ),
    TaskStatus.SCHEDULED: frozenset(
        {
            TaskStatus.PENDING,
            TaskStatus.RUNNING,
            TaskStatus.SKIPPED,
            TaskStatus.DROPPED,
            TaskStatus.FORCE_PASSED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.VERIFYING,
            TaskStatus.AWAITING_AMENDMENT,
            TaskStatus.RETRY_SCHEDULED,
            TaskStatus.FAILED,
            TaskStatus.DROPPED,
            TaskStatus.SKIPPED,
            TaskStatus.FORCE_PASSED,
        }
    ),
    TaskStatus.VERIFYING: frozenset(
        {
            TaskStatus.VERIFY_PASSED,
            TaskStatus.RETRY_SCHEDULED,
            TaskStatus.FAILED,
            TaskStatus.DROPPED,
            TaskStatus.SKIPPED,
            TaskStatus.FORCE_PASSED,
        }
    ),
    # verify_passed gains → dropped (Phase 4): a wave task verified green but
    # unresolvable at integration (conflict/semantic) is dropped from the wave.
    TaskStatus.VERIFY_PASSED: frozenset(
        {TaskStatus.COMPLETED, TaskStatus.RETRY_SCHEDULED, TaskStatus.DROPPED}
    ),
    TaskStatus.RETRY_SCHEDULED: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.FAILED,
            TaskStatus.DROPPED,
            TaskStatus.SKIPPED,
            TaskStatus.FORCE_PASSED,
        }
    ),
    TaskStatus.AWAITING_AMENDMENT: frozenset(
        {TaskStatus.RUNNING, TaskStatus.DROPPED, TaskStatus.SKIPPED, TaskStatus.FORCE_PASSED}
    ),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.SKIPPED: frozenset(),
    TaskStatus.FORCE_PASSED: frozenset(),
    TaskStatus.DROPPED: frozenset(),
}

# Attempt states (impl-plan §5.3). All non-initialized states are terminal —
# an attempt is disposable (D8); retries are new attempts.
_ATTEMPT_EDGES: dict[AttemptStatus, frozenset[AttemptStatus]] = {
    AttemptStatus.INITIALIZED: frozenset({AttemptStatus.RUNNING}),
    AttemptStatus.RUNNING: frozenset(
        {
            AttemptStatus.SUCCEEDED,
            AttemptStatus.FAILED,
            AttemptStatus.TIMEOUT,
            AttemptStatus.CRASHED,
            AttemptStatus.BUDGET_FROZEN,
            AttemptStatus.AMENDMENT_REQUESTED,
            AttemptStatus.INTEGRITY_VIOLATION,
        }
    ),
    AttemptStatus.SUCCEEDED: frozenset(),
    AttemptStatus.FAILED: frozenset(),
    AttemptStatus.TIMEOUT: frozenset(),
    AttemptStatus.CRASHED: frozenset(),
    AttemptStatus.BUDGET_FROZEN: frozenset(),
    AttemptStatus.AMENDMENT_REQUESTED: frozenset(),
    AttemptStatus.INTEGRITY_VIOLATION: frozenset(),
}

_EDGES: dict[str, dict[Any, frozenset[Any]]] = {
    "run": _RUN_EDGES,
    "task": _TASK_EDGES,
    "attempt": _ATTEMPT_EDGES,
}


class InvalidTransition(RuntimeError):
    def __init__(self, kind: str, current: str, requested: str) -> None:
        super().__init__(f"illegal {kind} transition: {current} -> {requested}")
        self.kind, self.current, self.requested = kind, current, requested


async def current_status(db: Database, kind: str, row_id: str) -> Any:
    table = _TABLE[kind]
    r = await db.fetchone(f"SELECT status FROM {table} WHERE id = ?", (row_id,))
    if r is None:
        raise KeyError(f"{kind} {row_id} not found")
    return r["status"]


async def transition(
    db: Database,
    kind: str,
    row_id: str,
    new_state: str,
    *,
    payload: dict[str, Any] | None = None,
) -> str:
    """Transition a run/task/attempt to *new_state*.

    Writes the status update and the ``state_transition`` audit event in one
    transaction. Returns the new state string. Raises :class:`InvalidTransition`
    for edges not present in the tables above.
    """
    if kind not in _EDGES:
        raise ValueError(f"unknown entity kind: {kind}")
    new = _coerce(kind, new_state)
    table = _TABLE[kind]

    # The whole check-and-set runs inside one BEGIN IMMEDIATE transaction so
    # two concurrent transitions from the same source state cannot both win.
    async with db.tx() as conn:
        async with conn.execute(f"SELECT status FROM {table} WHERE id = ?", (row_id,)) as cur:
            row = await cur.fetchone()
        if row is None:
            raise KeyError(f"{kind} {row_id} not found")
        current = _coerce(kind, row["status"])
        if new not in _EDGES[kind][current]:
            raise InvalidTransition(kind, current.value, new.value)

        event = {
            "entity": kind,
            "id": row_id,
            "from": current.value,
            "to": new.value,
            **(payload or {}),
        }
        run_id: str | None
        attempt_id: str | None
        if kind == "run":
            run_id, attempt_id = row_id, None
        elif kind == "task":
            run_id, attempt_id = await _task_context(conn, row_id), None
        else:
            run_id, attempt_id = await _attempt_context(conn, row_id), row_id

        await conn.execute(f"UPDATE {table} SET status = ? WHERE id = ?", (new.value, row_id))
        await conn.execute(
            "INSERT INTO agent_events (ts, event_type, run_id, attempt_id, payload_json)"
            " VALUES (?, 'state_transition', ?, ?, ?)",
            (utcnow_iso(), run_id, attempt_id, json.dumps(event)),
        )
    return str(new.value)


def _coerce(kind: str, value: str) -> Any:
    enum_cls = {"run": RunStatus, "task": TaskStatus, "attempt": AttemptStatus}[kind]
    return enum_cls(value)


async def _task_context(conn: Any, task_id: str) -> str | None:
    async with conn.execute(
        "SELECT v.run_id FROM tasks t JOIN waves v ON t.wave_id = v.id WHERE t.id = ?",
        (task_id,),
    ) as cur:
        r = await cur.fetchone()
    return r["run_id"] if r else None


async def _attempt_context(conn: Any, attempt_id: str) -> str | None:
    async with conn.execute(
        "SELECT v.run_id FROM attempts a"
        " JOIN tasks t ON a.task_id = t.id"
        " JOIN waves v ON t.wave_id = v.id"
        " WHERE a.id = ?",
        (attempt_id,),
    ) as cur:
        r = await cur.fetchone()
    return r["run_id"] if r else None


# ------------------------------------------------------------------ boot self-test


class FsmSelfTestError(RuntimeError):
    """Raised by :func:`self_test` when the edge tables and the engine disagree."""

    def __init__(self, failures: list[str]) -> None:
        super().__init__(
            "FSM self-test failed: " + "; ".join(failures[:10])
            + (f" (+{len(failures) - 10} more)" if len(failures) > 10 else "")
        )
        self.failures = failures


async def self_test(db_path: Path | None = None) -> None:
    """Boot-time self-test (impl-plan §6.3 / Phase 0 exit criterion 1).

    Walks a synthetic run/task/attempt through every declared edge against a
    throwaway database (``*db_path*`` or a fresh temp file) and asserts every
    undeclared edge raises :class:`InvalidTransition`. Raises
    :class:`FsmSelfTestError` on any disagreement between the tables and the
    guarded transition engine — the daemon and the web app call this once at
    startup to fail fast rather than mid-run.
    """
    tmp: tempfile.TemporaryDirectory[str] | None = None
    if db_path is None:
        tmp = tempfile.TemporaryDirectory(prefix="girder-fsm-selftest-")
        db_path = Path(tmp.name) / "fsm.db"
    db = await Database.open(db_path, migrations_dir=default_migrations_dir())
    failures: list[str] = []
    try:
        project = await repo.create_project(db, "fsm-selftest", "/fsm-selftest")
        run = await repo.create_run(db, project.id, "boot self-test", "run/fsm-selftest", 0.0)
        wave = await repo.get_or_create_wave0(db, run.id)
        task = await repo.create_task(db, wave.id, 0, "boot self-test", TaskType.CODE_CHANGE)
        attempt = await repo.create_attempt(db, task.id, "0" * 40)
        plan: list[tuple[str, str, str, Any, dict[Any, frozenset[Any]]]] = [
            ("run", "runs", run.id, RunStatus, _RUN_EDGES),
            ("task", "tasks", task.id, TaskStatus, _TASK_EDGES),
            ("attempt", "attempts", attempt.id, AttemptStatus, _ATTEMPT_EDGES),
        ]
        for kind, table, row_id, enum_cls, edges in plan:
            legal = {(f.value, t.value) for f, targets in edges.items() for t in targets}
            states = [s.value for s in enum_cls]
            for src in states:
                for dst in states:
                    # Direct write before EVERY probe: a successful transition
                    # must not leak into the next one's source state.
                    await db.execute(f"UPDATE {table} SET status = ? WHERE id = ?", (src, row_id))
                    await db.conn.commit()
                    try:
                        await transition(db, kind, row_id, dst)
                        ok = (src, dst) in legal
                    except InvalidTransition:
                        ok = (src, dst) not in legal
                    if not ok:
                        failures.append(f"{kind} {src}->{dst} disagrees with the edge table")
        if failures:
            raise FsmSelfTestError(failures)
    finally:
        await db.close()
        if tmp is not None:
            tmp.cleanup()


# Convenience wrappers keep call sites readable.
async def transition_run(db: Database, run_id: str, new: RunStatus | str, **payload: Any) -> str:
    return await transition(db, "run", run_id, str(new), payload=payload or None)


async def transition_task(db: Database, task_id: str, new: TaskStatus | str, **payload: Any) -> str:
    return await transition(db, "task", task_id, str(new), payload=payload or None)


async def transition_attempt(
    db: Database, attempt_id: str, new: AttemptStatus | str, **payload: Any
) -> str:
    return await transition(db, "attempt", attempt_id, str(new), payload=payload or None)


__all__ = [
    "_ATTEMPT_EDGES",
    "_RUN_EDGES",
    "_TASK_EDGES",
    "FsmSelfTestError",
    "InvalidTransition",
    "current_status",
    "self_test",
    "transition",
    "transition_attempt",
    "transition_run",
    "transition_task",
]
