"""Run pages, run JSON, DAG graph, and the SSE event stream (WP 10.1).

Owns: ``GET /runs/{rid}``, ``GET /runs/{rid}/panel``, ``GET /api/runs/{rid}``,
``GET /api/runs/{rid}/diffs``, ``GET /api/runs/{rid}/spend``,
``GET /api/runs/{rid}/graph``, and the SSE stream ``GET /api/runs/{rid}/events``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from girder.api.app import render
from girder.api.deps import Db, RedactorDeps
from girder.api.routes._shared import (
    panel_context,
    redact,
    redact_value,
    require_run,
    run_context,
)
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import (
    TERMINAL_RUN_STATUSES,
    Run,
    RunStatus,
    Task,
    TaskStatus,
)
from girder.guard.redact import Redactor

router = APIRouter()


@router.get("/runs/{rid}", response_class=HTMLResponse)
async def run_page(request: Request, db: Db, redactor: RedactorDeps, rid: str) -> HTMLResponse:
    ctx = await run_context(db, rid, redactor_=redactor)
    run: Run = ctx["run"]  # type: ignore[assignment]
    from girder.api.routes.specs import _estimate_ctx

    ctx["graph"] = await _graph(db, run)
    ctx.update(_estimate_ctx(request.app, run, ctx["project"]))  # type: ignore[arg-type]
    ctx = panel_context(run, ctx)
    return render(request, "run.html", ctx)


@router.get("/runs/{rid}/panel", response_class=HTMLResponse)
async def run_panel(
    request: Request, db: Db, redactor: RedactorDeps, rid: str
) -> HTMLResponse:
    from girder.api.routes.specs import _estimate_ctx

    base = await run_context(db, rid, redactor_=redactor)
    base.update(_estimate_ctx(request.app, base["run"], base["project"]))  # type: ignore[arg-type]
    return render(request, "_run_panel.html", panel_context(base["run"], base))  # type: ignore[arg-type]


@router.get("/api/runs/{rid}")
async def run_json(request: Request, db: Db, rid: str) -> JSONResponse:
    run = await require_run(db, rid)
    return JSONResponse(
        {
            "id": run.id,
            "status": run.status.value,
            "spec_hash": run.spec_hash,
            "branch": run.branch,
            "spend_usd": run.spend_usd,
            "projected_spend_usd": run.projected_spend_usd,
            "budget_cap_usd": run.budget_cap_usd,
            "has_proposal": run.proposal_md is not None,
        }
    )


@router.get("/api/runs/{rid}/diffs")
async def run_diffs(
    request: Request, db: Db, redactor: RedactorDeps, rid: str
) -> JSONResponse:
    """Persisted attempt diffs of a run (§10 diff viewer), re-redacted through
    the same redactor as the live event stream (plan §10)."""
    await require_run(db, rid)
    diffs = await repo.list_attempt_diffs_for_run(db, rid)
    return JSONResponse(
        {
            "diffs": [
                {
                    "attempt_id": d.attempt_id,
                    "task_id": d.task_id,
                    "base_commit": d.base_commit,
                    "head_commit": d.head_commit,
                    "diff": redact(redactor, d.diff_redacted),
                    "created_at": d.created_at,
                }
                for d in diffs
            ]
        }
    )


@router.get("/api/runs/{rid}/spend")
async def run_spend(request: Request, db: Db, rid: str) -> JSONResponse:
    run = await require_run(db, rid)
    usage = await repo.list_token_usage_for_run(db, rid)
    roles = await repo.sum_token_usage_for_run(db, rid)
    per_task = await repo.sum_token_usage_by_task_for_run(db, rid)
    violations = await repo.list_integrity_violations_for_run(db, rid)
    return JSONResponse(
        {
            "cap": run.budget_cap_usd,
            "spend_usd": run.spend_usd,
            "projected_spend_usd": run.projected_spend_usd,
            "usage": usage,
            "roles": roles,
            "per_task": per_task,
            "violations": len(violations),
        }
    )


# ------------------------------------------------------- Sprint 6: live console


_STREAM_END_STATUSES = frozenset(TERMINAL_RUN_STATUSES) | {
    RunStatus.BUDGET_EXHAUSTED,
    RunStatus.MERGE_PENDING_HUMAN,
}

# Event-class rules tuned against the actual insert_event call sites:
# violations land in agent_events as budget_* (src/girder/models/gateway.py),
# scope/integrity violations carry "violation" in the type, and steering_abort /
# attempt_failed are spec vocabulary; state machinery emits state_transition
# (src/girder/fsm.py), steering_* (src/girder/orchestrator/run_engine.py),
# wave_* (scheduler/integrator/run_engine), task_* and attempt_* (task_engine /
# agent/runtime); tool traffic is tool_result / turn_start (agent/runtime).
_STATE_EVENT_TYPES = frozenset(
    {
        "state_transition",
        "steering_pause",
        "steering_resume",
        "steering_requested",
        "steering_applied",
        "steering_ignored",
        "steering_injected",
        "verify_failed",
    }
)
_TOOL_EVENT_TYPES = frozenset({"tool_call", "tool_result", "turn_start", "compaction"})


def _event_class(event_type: str) -> str:
    """Classify an agent_event for the SSE stream (impl-plan §10).

    Issue 22: budget_* events get their own ``budget`` class (amber) — they are
    budget warnings, not boundary violations; *violation* / steering_abort /
    attempt_failed stay ``violation`` (red).
    """
    if "violation" in event_type or event_type in {"steering_abort", "attempt_failed"}:
        return "violation"
    if event_type.startswith("budget_"):
        return "budget"
    if (
        event_type in _STATE_EVENT_TYPES
        or event_type.startswith("wave_")
        or event_type.startswith("run_")
        or event_type.startswith("task_")
        or event_type.startswith("attempt_")
        or event_type.startswith("steering_")
    ):
        return "state"
    if event_type in _TOOL_EVENT_TYPES:
        return "tool"
    return "info"


@router.get("/api/runs/{rid}/events")
async def run_events(
    request: Request,
    db: Db,
    redactor: RedactorDeps,
    rid: str,
    after_id: int = 0,
    poll_s: float = 1.0,
    max_ticks: int = 7200,
) -> StreamingResponse:
    """Server-Sent Events stream of a run's agent_events (impl-plan §10).

    Tail-polls the AUTOINCREMENT id; violations arrive as a distinct event
    class. Terminal-ish runs (merged/failed/aborted, budget_exhausted,
    merge_pending_human) end the stream with an ``end`` event — the browser's
    EventSource reconnects automatically on plain close.
    """
    await require_run(db, rid)
    poll = max(poll_s, 0.05)

    async def gen() -> AsyncIterator[str]:
        last = after_id
        silent_ticks = 0
        try:
            for _ in range(max(1, max_ticks)):
                rows = await repo.list_events_for_run(db, rid, after_id=last)
                for row in rows:
                    last = int(row["id"])
                    payload = redact_value(redactor, json.loads(row["payload_json"]))
                    data = {
                        "id": row["id"],
                        "ts": row["ts"],
                        "event_type": row["event_type"],
                        "attempt_id": row["attempt_id"],
                        "payload": payload,
                    }
                    yield (
                        f"event: {_event_class(row['event_type'])}\n"
                        f"data: {json.dumps(data)}\n\n"
                    )
                run = await repo.get_run(db, rid)
                if run is not None and run.status in _STREAM_END_STATUSES:
                    yield f"event: end\ndata: {json.dumps({'status': run.status.value})}\n\n"
                    return
                if not rows:
                    silent_ticks += 1
                    if silent_ticks % 15 == 0:
                        # SSE comment frame: EventSource ignores it, but it
                        # pushes bytes through intermediaries that would
                        # otherwise see an idle stream and buffer or drop it,
                        # and proves the socket is alive while nothing happens.
                        yield ": keepalive\n\n"
                    await asyncio.sleep(poll)
        except asyncio.CancelledError:  # client disconnected
            return

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ------------------------------------------------------------------ DAG (graph)


_TASK_STATUS_CLASSES: dict[TaskStatus, str] = {
    TaskStatus.PENDING: "pending",
    TaskStatus.SCHEDULED: "running",
    TaskStatus.RUNNING: "running",
    TaskStatus.VERIFYING: "verifying",
    TaskStatus.VERIFY_PASSED: "done",
    TaskStatus.RETRY_SCHEDULED: "retry",
    TaskStatus.AWAITING_AMENDMENT: "amendment",
    TaskStatus.COMPLETED: "done",
    TaskStatus.FAILED: "failed",
    TaskStatus.SKIPPED: "slate",
    TaskStatus.FORCE_PASSED: "slate",
    TaskStatus.DROPPED: "slate",
}


def _task_view(t: Task) -> dict[str, Any]:
    return {
        "id": t.id,
        "seq": t.seq,
        "title": t.title,
        "task_type": t.task_type.value,
        "status": t.status.value,
        "scope_globs": t.scope_globs,
        "depends_on": t.depends_on,
        "cls": _TASK_STATUS_CLASSES.get(t.status, "pending"),
    }


async def _graph(db: Database, run: Run) -> dict[str, Any]:
    """DAG shape for the run page viz and /api/runs/{rid}/graph."""
    waves = sorted(await repo.list_waves_for_run(db, run.id), key=lambda w: w.sequence_order)
    by_wave: dict[str, list[dict[str, Any]]] = {}
    unwaved: list[dict[str, Any]] = []
    wave_ids = {w.id for w in waves}
    for t in await repo.list_tasks_for_run(db, run.id):
        if t.wave_id in wave_ids:
            by_wave.setdefault(t.wave_id, []).append(_task_view(t))
        else:
            unwaved.append(_task_view(t))
    return {
        "run_id": run.id,
        "status": run.status.value,
        "paused": run.paused,
        "waves": [
            {
                "id": w.id,
                "sequence_order": w.sequence_order,
                "status": w.status,
                "tasks": by_wave.get(w.id, []),
            }
            for w in waves
        ],
        "unwaved_tasks": unwaved,
    }


@router.get("/api/runs/{rid}/graph")
async def run_graph(request: Request, db: Db, rid: str) -> JSONResponse:
    run = await require_run(db, rid)
    return JSONResponse(await _graph(db, run))
