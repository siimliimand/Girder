"""History and post-mortem views (WP 10.1).

Owns: ``GET /api/history``, ``GET /history``, ``GET /runs/{rid}/postmortem``.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from girder.api.app import render
from girder.api.deps import Db, RedactorDeps
from girder.api.routes._shared import (
    excerpt,
    redact,
    redact_value,
    require_project,
    require_run,
)
from girder.db import repo
from girder.db.engine import Database
from girder.guard.redact import Redactor

router = APIRouter()


async def _history(db: Database) -> list[dict[str, Any]]:
    projects = {p.id: p for p in await repo.list_projects(db)}
    rows: list[dict[str, Any]] = []
    for pid, project in projects.items():
        for r in await repo.list_runs_for_project(db, pid):
            rows.append(
                {
                    "run_id": r.id,
                    "project_id": pid,
                    "project_name": project.name,
                    "status": r.status.value,
                    "paused": r.paused,
                    "branch": r.branch,
                    "spend_usd": r.spend_usd,
                    "projected_spend_usd": r.projected_spend_usd,
                    "budget_cap_usd": r.budget_cap_usd,
                    "integrity_violations": r.integrity_violations,
                }
            )
    return rows


@router.get("/api/history")
async def history(request: Request, db: Db) -> JSONResponse:
    return JSONResponse({"runs": await _history(db)})


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request, db: Db) -> HTMLResponse:
    return render(request, "history.html", {"runs": await _history(db)})


@router.get("/runs/{rid}/postmortem", response_class=HTMLResponse)
async def postmortem(request: Request, db: Db, redactor: RedactorDeps, rid: str) -> HTMLResponse:
    """Historical post-mortem view (WP 6.5). Every dynamic string is re-run
    through the redactor before rendering (plan §10)."""
    run = await require_run(db, rid)
    project = await require_project(db, run.project_id)
    events = await repo.list_events_for_run(db, rid, after_id=0, limit=100000)
    # Diff viewer (§10): stored diffs re-redacted through the same redactor as
    # the live path (plan §10).
    diff_by_attempt = {
        d.attempt_id: redact(redactor, d.diff_redacted) or ""
        for d in await repo.list_attempt_diffs_for_run(db, rid)
    }

    task_views: list[dict[str, Any]] = []
    for t in await repo.list_tasks_for_run(db, rid):
        attempt_views: list[dict[str, Any]] = []
        for a in await repo.list_attempts_for_task(db, t.id):
            tool_calls = [
                {
                    "ts": c["ts"],
                    "tool_name": c["tool_name"],
                    "duration_ms": c["duration_ms"],
                    "scope_violation": bool(c["scope_violation"]),
                    "held": bool(c["held"]),
                    "verdict": c.get("verdict"),
                    "input_json": redact(redactor, c["input_json"]),
                    "output_excerpt": redact(
                        redactor, excerpt(c["output_blob_redacted"])
                    ),
                }
                for c in await repo.list_tool_calls_for_attempt(db, a.id)
            ]
            prompts = [
                {
                    "turn": p.turn,
                    "role": p.role,
                    "content": redact(redactor, p.content_redacted),
                    "ts": p.ts,
                }
                for p in await repo.list_attempt_prompts(db, a.id)
            ]
            attempt_views.append(
                {
                    "attempt": a,
                    "diff": diff_by_attempt.get(a.id),
                    "tool_calls": tool_calls,
                    "prompts": prompts,
                    "usage": await repo.list_token_usage_for_attempt(db, a.id),
                    "events": [
                        {
                            "ts": e["ts"],
                            "event_type": e["event_type"],
                            "payload": redact_value(
                                redactor, json.loads(e["payload_json"])
                            ),
                        }
                        for e in events
                        if e["attempt_id"] == a.id
                    ],
                }
            )
        task_views.append({"task": t, "attempts": attempt_views})

    violations = [
        {
            "id": v["id"],
            "kind": v["kind"],
            "ts": v["ts"],
            "task_id": v["task_id"],
            "attempt_id": v["attempt_id"],
            "detail": redact_value(redactor, json.loads(v["detail_json"])),
        }
        for v in await repo.list_integrity_violations_for_run(db, rid)
    ]
    notifications = [
        {**n, "payload_redacted": redact(redactor, n["payload_redacted"])}
        for n in await repo.list_notifications_for_run(db, rid)
    ]
    amendments = [
        {
            "id": a.id,
            "reason": redact(redactor, a.reason),
            "suggested_change": redact(redactor, a.suggested_change),
            "status": a.status,
            "guidance": redact(redactor, a.guidance),
        }
        for a in await repo.list_amendments_for_run(db, rid)
    ]
    return render(
        request,
        "postmortem.html",
        {
            "run": run,
            "project": project,
            "tasks": task_views,
            "violations": violations,
            "notifications": notifications,
            "amendments": amendments,
            "usage": await repo.list_token_usage_for_run(db, rid),
            "roles": await repo.sum_token_usage_for_run(db, rid),
            "spend": {
                "cap": run.budget_cap_usd,
                "spend_usd": run.spend_usd,
                "projected_spend_usd": run.projected_spend_usd,
            },
        },
    )
