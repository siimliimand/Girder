"""Intent clarification loop (WS-04 / §8.5).

Owns: ``GET /runs/{rid}/clarify`` (question form) and
``POST /api/runs/{rid}/clarify`` (answers → enriched intent → spec
generation), plus the CLARIFYING parking helpers used at run creation.

Additive section: the default flow is untouched when ``clarify`` is unset
and ``[specs] require_clarification`` is false.

NOTE (coordination): clarification-session SQL lives here as private
helpers because db/repo.py was outside WS-04's wave-1 file ownership;
move these into girder.db.repo at a later wave if preferred.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from girder.api.app import dispatch_generation, render
from girder.api.deps import Db
from girder.api.routes._shared import panel_context, require_run, run_context
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run, RunStatus
from girder.fsm import transition_run
from girder.specs.generator import SpecGenerationError, _intent_with_clarification
from girder.util import new_id, utcnow_iso

router = APIRouter()


async def _insert_clarification_session(db: Database, run_id: str, questions: list[str]) -> None:
    async with db.tx() as conn:
        await conn.execute(
            "INSERT INTO clarification_sessions (id, run_id, questions_json, created_at)"
            " VALUES (?, ?, ?, ?)",
            (new_id(), run_id, json.dumps(questions), utcnow_iso()),
        )


async def _latest_open_session(db: Database, run_id: str) -> Any | None:
    return await db.fetchone(
        "SELECT * FROM clarification_sessions"
        " WHERE run_id = ? AND answers_json IS NULL ORDER BY rowid DESC LIMIT 1",
        (run_id,),
    )


async def _start_clarification(
    request: Request, app: Any, project: Project, run: Run
) -> RedirectResponse | HTMLResponse:
    """Park *run* in CLARIFYING with generated questions (§8.5)."""
    db: Database = app.state.db
    await transition_run(db, run.id, RunStatus.CLARIFYING, reason="clarification_requested")
    try:
        generator = app.state.create_spec_generator()
        questions = await generator.generate_clarification_questions(run=run)
    except SpecGenerationError as exc:
        await repo.insert_event(
            db,
            "clarification_failed",
            {"error": "; ".join(exc.errors)[:2000]},
            run_id=run.id,
        )
        return render(
            request,
            "project.html",
            {
                "project": project,
                "runs": await repo.list_runs_for_project(db, project.id),
                "error": "clarification failed: " + "; ".join(exc.errors)[:500],
            },
            status_code=502,
        )
    await _insert_clarification_session(db, run.id, questions)
    await repo.insert_event(
        db, "clarification_requested", {"questions": len(questions)}, run_id=run.id
    )
    return RedirectResponse(f"/runs/{run.id}/clarify", status_code=303)


@router.get("/runs/{rid}/clarify", response_class=HTMLResponse, response_model=None)
async def clarify_page(request: Request, db: Db, rid: str) -> HTMLResponse | RedirectResponse:
    run = await require_run(db, rid)
    session = await _latest_open_session(db, rid)
    if run.status != RunStatus.CLARIFYING or session is None:
        return RedirectResponse(f"/runs/{rid}", status_code=303)
    questions: list[str] = json.loads(str(session["questions_json"]))
    return render(request, "clarify.html", {"run": run, "questions": questions})


@router.post("/api/runs/{rid}/clarify", response_model=None)
async def submit_clarification(
    request: Request, db: Db, rid: str, answers: Annotated[list[str], Form(...)]
) -> HTMLResponse | RedirectResponse:
    app = request.app
    run = await require_run(db, rid)
    base = await run_context(db, rid, redactor_=app.state.redactor)
    session = await _latest_open_session(db, rid)
    if run.status != RunStatus.CLARIFYING or session is None:
        return render(
            request,
            "_run_panel.html",
            panel_context(
                run,
                {**base, "errors": [f"cannot answer clarification: run is {run.status.value}"]},
            ),
            status_code=409,
        )
    questions: list[str] = json.loads(str(session["questions_json"]))
    stripped = [a.strip() for a in answers]
    if len(stripped) != len(questions) or any(not a for a in stripped):
        return render(
            request,
            "clarify.html",
            {
                "run": run,
                "questions": questions,
                "answers": stripped,
                "error": "Every question needs a non-empty answer.",
            },
            status_code=400,
        )
    clarification = dict(zip(questions, stripped, strict=True))
    async with db.tx() as conn:
        await conn.execute(
            "UPDATE clarification_sessions SET answers_json = ? WHERE id = ?",
            (json.dumps(stripped), session["id"]),
        )
    # Enrich the persisted intent with the answers so the dispatched
    # generation (which reads run.intent) sees the full picture; the
    # generator applies the same enrichment when given a clarification map.
    enriched = _intent_with_clarification(run.intent, clarification)
    # (Direct SQL: runs.intent is not in _RUN_WRITABLE_FIELDS, and db/repo
    # was outside WS-04's wave-1 ownership — see the module note above.)
    async with db.tx() as conn:
        await conn.execute(
            "UPDATE runs SET intent = ?, updated_at = ? WHERE id = ?",
            (enriched, utcnow_iso(), rid),
        )
    await transition_run(db, rid, RunStatus.DRAFT, reason="clarification_answered")
    await transition_run(db, rid, RunStatus.SPEC_PENDING, reason="spec_generation_dispatched")
    await repo.insert_event(db, "clarification_answered", {"questions": len(questions)}, run_id=rid)
    dispatch_generation(app, rid, None)
    return RedirectResponse(f"/runs/{rid}", status_code=303)
