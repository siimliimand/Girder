"""Spec-amendment resolution and the amendments inbox (WP 10.1).

Owns: ``POST /api/runs/{rid}/amendments/{aid}/approve|reject|abort``,
``GET /api/runs/{rid}/amendments``, ``GET /amendments``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from girder.api.app import render
from girder.api.deps import Db, NotifierDeps
from girder.notify.notifier import Notifier
from girder.api.routes._shared import (
    excerpt,
    panel_context,
    require_run,
    run_context,
)
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run
from girder.specs.amendment import AmendmentError, resolve_amendment

router = APIRouter()


@router.post("/api/runs/{rid}/amendments/{aid}/approve", response_model=None)
async def approve_amendment(
    request: Request, db: Db, notifier: NotifierDeps, rid: str, aid: str, scope_globs: str = Form("")
) -> HTMLResponse | RedirectResponse:
    # §8.4: an approval may widen the amended task's write scope. Globs arrive
    # newline/comma-separated; blank input means "no scope change".
    raw = [g.strip() for chunk in scope_globs.splitlines() for g in chunk.split(",")]
    globs = [g for g in raw if g] or None
    return await _resolve_amendment_route(
        request, db, rid, aid, "approved", scope_globs=globs, notifier=notifier
    )


@router.post("/api/runs/{rid}/amendments/{aid}/reject", response_model=None)
async def reject_amendment(
    request: Request,
    db: Db,
    aid: str,
    notifier: NotifierDeps,
    rid: str,
    guidance: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    return await _resolve_amendment_route(
        request, db, rid, aid, "rejected", guidance=guidance.strip() or None, notifier=notifier
    )


@router.post("/api/runs/{rid}/amendments/{aid}/abort", response_model=None)
async def abort_amendment(
    request: Request, db: Db, rid: str, aid: str
) -> HTMLResponse | RedirectResponse:
    return await _resolve_amendment_route(request, db, rid, aid, "aborted")


async def _resolve_amendment_route(
    request: Request,
    db: Database,
    rid: str,
    aid: str,
    decision: str,
    *,
    guidance: str | None = None,
    scope_globs: list[str] | None = None,
    notifier: Notifier | None = None,
) -> HTMLResponse | RedirectResponse:
    base = await run_context(db, rid)
    run: Run = base["run"]  # type: ignore[assignment]
    project: Project = base["project"]  # type: ignore[assignment]
    amendment = await repo.get_spec_amendment(db, aid)
    if amendment is None or amendment.run_id != rid:
        raise HTTPException(status_code=404, detail="amendment not found")
    try:
        await resolve_amendment(
            db,
            project=project,
            run=run,
            amendment=amendment,
            decision=decision,
            guidance=guidance,
            scope_globs=scope_globs,
            notifier=notifier,
        )
    except AmendmentError as exc:
        return render(
            request,
            "_run_panel.html",
            panel_context(run, {**base, "errors": [str(exc)]}),
            status_code=409,
        )
    # Actions taken from the §10 Amendments inbox bounce back to their Referer
    # (the inbox); run-page actions have no Referer and stay on the run page.
    return RedirectResponse(request.headers.get("Referer") or f"/runs/{rid}", status_code=303)


@router.get("/api/runs/{rid}/amendments")
async def run_amendments(request: Request, db: Db, rid: str) -> JSONResponse:
    await require_run(db, rid)
    amendments = await repo.list_amendments_for_run(db, rid)
    return JSONResponse(
        {
            "amendments": [
                {
                    "id": a.id,
                    "run_id": a.run_id,
                    "task_id": a.task_id,
                    "reason": a.reason,
                    "suggested_change": a.suggested_change,
                    "status": a.status,
                    "guidance": a.guidance,
                    "new_spec_hash": a.new_spec_hash,
                    "resolved_at": a.resolved_at,
                }
                for a in amendments
            ]
        }
    )


async def _amendments_inbox(db: Database) -> list[dict[str, Any]]:
    """Pending amendments across all runs with run/project context (§10)."""
    projects = {p.id: p for p in await repo.list_projects(db)}
    rows: list[dict[str, Any]] = []
    for a in await repo.list_pending_amendments(db):
        r = await repo.get_run(db, a.run_id)
        project = projects[r.project_id] if r is not None and r.project_id in projects else None
        rows.append(
            {
                "amendment_id": a.id,
                "run_id": a.run_id,
                "project_id": project.id if project else None,
                "project_name": project.name if project else a.run_id,
                "intent": excerpt(r.intent, 80) if r else None,
                "task_id": a.task_id,
                "reason": a.reason,
                "suggested_change": a.suggested_change,
            }
        )
    return rows


@router.get("/amendments", response_class=HTMLResponse)
async def amendments_page(request: Request, db: Db) -> HTMLResponse:
    return render(request, "amendments.html", {"pending": await _amendments_inbox(db)})
