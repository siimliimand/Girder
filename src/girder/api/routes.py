"""Routes for the approval web console (impl-plan §10).

SQL lives only in :mod:`girder.db`; everything here goes through repo fns and
:mod:`girder.fsm`. Generation runs as a tracked background task (see
:func:`girder.api.app.dispatch_generation`).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from girder.api.app import dispatch_generation, render
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run, RunStatus
from girder.fsm import transition_run
from girder.specs.amendment import AmendmentError, resolve_amendment
from girder.specs.freeze import FreezeError, approve_and_freeze
from girder.specs.validator import SpecValidationError, parse_spec

router = APIRouter()


async def _require_run(db: Database, run_id: str) -> Run:
    run = await repo.get_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


async def _require_project(db: Database, project_id: str) -> Project:
    project = await repo.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


async def _run_context(db: Database, run_id: str) -> dict[str, object]:
    """Everything run.html and _run_panel.html need."""
    run = await _require_run(db, run_id)
    project = await _require_project(db, run.project_id)
    usage = await repo.list_token_usage_for_run(db, run_id)
    failure = await repo.get_latest_event(db, run_id, "spec_generation_failed")
    spend = {
        "cap": run.budget_cap_usd,
        "spend_usd": run.spend_usd,
        "projected_spend_usd": run.projected_spend_usd,
    }
    pending_amendment = await repo.get_pending_amendment(db, run_id)
    return {
        "run": run,
        "project": project,
        "spend": spend,
        "usage": usage,
        "generation_error": failure,
        "pending_amendment": pending_amendment,
    }


def _panel_context(run: Run, base: dict[str, object]) -> dict[str, object]:
    ctx = dict(base)
    poll = (
        run.status == RunStatus.SPEC_PENDING
        and run.proposal_md is None
        and ctx.get("generation_error") is None
    )
    ctx["poll"] = poll
    return ctx


# ----------------------------------------------------------------------- pages


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    db: Database = request.app.state.db
    projects = await repo.list_projects(db)
    return render(request, "index.html", {"projects": projects})


@router.post("/api/projects", response_model=None)
async def create_project(
    request: Request, name: str = Form(...), repo_path: str = Form(...)
) -> HTMLResponse | RedirectResponse:
    db: Database = request.app.state.db
    projects = await repo.list_projects(db)
    error: str | None = None
    if not name.strip():
        error = "Project name must not be empty."
    elif await repo.get_project_by_name(db, name) is not None:
        error = f"A project named {name!r} already exists."
    elif not Path(repo_path).is_dir():  # noqa: ASYNC240 - host-path form validation
        error = f"repo_path {repo_path!r} is not an existing directory."
    if error is not None:
        return render(
            request,
            "index.html",
            {"projects": projects, "error": error, "form": {"name": name, "repo_path": repo_path}},
            status_code=400,
        )
    await repo.create_project(db, name.strip(), repo_path)
    return RedirectResponse("/", status_code=303)


@router.get("/projects/{pid}", response_class=HTMLResponse)
async def project_page(request: Request, pid: str) -> HTMLResponse:
    db: Database = request.app.state.db
    project = await _require_project(db, pid)
    runs = await repo.list_runs_for_project(db, pid)
    return render(request, "project.html", {"project": project, "runs": runs})


@router.post("/api/projects/{pid}/runs", response_model=None)
async def create_run(
    request: Request, pid: str, intent: str = Form(...)
) -> RedirectResponse | HTMLResponse:
    app = request.app
    db: Database = app.state.db
    await _require_project(db, pid)
    if not intent.strip():
        project = await _require_project(db, pid)
        return render(
            request,
            "project.html",
            {
                "project": project,
                "runs": await repo.list_runs_for_project(db, pid),
                "error": "Intent must not be empty.",
            },
            status_code=400,
        )
    settings = app.state.settings
    run = await repo.create_run(
        db, pid, intent, branch="pending", budget_cap_usd=settings.budget.run_cap_usd
    )
    await repo.update_run_fields(db, run.id, branch=f"run/{run.id[:8]}")
    await transition_run(db, run.id, RunStatus.SPEC_PENDING, reason="spec_generation_dispatched")
    dispatch_generation(app, run.id, None)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


@router.get("/runs/{rid}", response_class=HTMLResponse)
async def run_page(request: Request, rid: str) -> HTMLResponse:
    db: Database = request.app.state.db
    ctx = await _run_context(db, rid)
    ctx = _panel_context(ctx["run"], ctx)  # type: ignore[arg-type]
    return render(request, "run.html", ctx)


@router.get("/runs/{rid}/panel", response_class=HTMLResponse)
async def run_panel(request: Request, rid: str) -> HTMLResponse:
    db: Database = request.app.state.db
    base = await _run_context(db, rid)
    return render(request, "_run_panel.html", _panel_context(base["run"], base))  # type: ignore[arg-type]


# --------------------------------------------------------------------- actions


@router.post("/api/runs/{rid}/approve", response_model=None)
async def approve(request: Request, rid: str) -> HTMLResponse | RedirectResponse:
    db: Database = request.app.state.db
    base = await _run_context(db, rid)
    run: Run = base["run"]  # type: ignore[assignment]
    project: Project = base["project"]  # type: ignore[assignment]
    if run.spec_hash is not None:
        return render(
            request,
            "_run_panel.html",
            _panel_context(run, {**base, "errors": ["run is already frozen"]}),
            status_code=400,
        )
    if run.status != RunStatus.SPEC_PENDING or not run.proposal_md:
        return render(
            request,
            "_run_panel.html",
            _panel_context(
                run,
                {
                    **base,
                    "errors": [
                        f"cannot approve: run status is {run.status.value}"
                        + ("" if run.proposal_md else " and no proposal exists")
                    ],
                },
            ),
            status_code=400,
        )
    try:
        await approve_and_freeze(
            db,
            project=project,
            run=run,
            proposal_text=run.proposal_md,
            repo_path=Path(project.repo_path),
        )
    except SpecValidationError as exc:
        return render(
            request,
            "_run_panel.html",
            _panel_context(run, {**base, "errors": exc.errors}),
            status_code=400,
        )
    except FreezeError as exc:
        return render(
            request,
            "_run_panel.html",
            _panel_context(run, {**base, "errors": [str(exc)]}),
            status_code=400,
        )
    return RedirectResponse(f"/runs/{rid}", status_code=303)


@router.post("/api/runs/{rid}/edit", response_model=None)
async def edit(
    request: Request, rid: str, proposal: str = Form(...)
) -> HTMLResponse | RedirectResponse:
    db: Database = request.app.state.db
    base = await _run_context(db, rid)
    run: Run = base["run"]  # type: ignore[assignment]
    try:
        parse_spec(proposal)
    except SpecValidationError as exc:
        return render(
            request,
            "_run_panel.html",
            _panel_context(run, {**base, "errors": exc.errors}),
            status_code=400,
        )
    await repo.update_run_fields(db, rid, proposal_md=proposal)
    return RedirectResponse(f"/runs/{rid}", status_code=303)


@router.post("/api/runs/{rid}/regenerate", response_model=None)
async def regenerate(
    request: Request, rid: str, feedback: str = Form("")
) -> HTMLResponse | RedirectResponse:
    app = request.app
    db: Database = app.state.db
    base = await _run_context(db, rid)
    run: Run = base["run"]  # type: ignore[assignment]
    if run.status != RunStatus.SPEC_PENDING:
        return render(
            request,
            "_run_panel.html",
            _panel_context(
                run, {**base, "errors": [f"cannot regenerate: status is {run.status.value}"]}
            ),
            status_code=400,
        )
    await repo.update_run_fields(db, rid, proposal_md=None)
    await transition_run(db, rid, RunStatus.SPEC_PENDING, reason="regenerate")
    dispatch_generation(app, rid, feedback.strip() or None)
    return RedirectResponse(f"/runs/{rid}", status_code=303)


# ------------------------------------------------------- spec amendment actions


@router.post("/api/runs/{rid}/amendments/{aid}/approve", response_model=None)
async def approve_amendment(
    request: Request, rid: str, aid: str
) -> HTMLResponse | RedirectResponse:
    return await _resolve_amendment_route(request, rid, aid, "approved")


@router.post("/api/runs/{rid}/amendments/{aid}/reject", response_model=None)
async def reject_amendment(
    request: Request, rid: str, aid: str, guidance: str = Form("")
) -> HTMLResponse | RedirectResponse:
    return await _resolve_amendment_route(
        request, rid, aid, "rejected", guidance=guidance.strip() or None
    )


@router.post("/api/runs/{rid}/amendments/{aid}/abort", response_model=None)
async def abort_amendment(request: Request, rid: str, aid: str) -> HTMLResponse | RedirectResponse:
    return await _resolve_amendment_route(request, rid, aid, "aborted")


async def _resolve_amendment_route(
    request: Request,
    rid: str,
    aid: str,
    decision: str,
    *,
    guidance: str | None = None,
) -> HTMLResponse | RedirectResponse:
    db: Database = request.app.state.db
    base = await _run_context(db, rid)
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
            notifier=request.app.state.notifier,
        )
    except AmendmentError as exc:
        return render(
            request,
            "_run_panel.html",
            _panel_context(run, {**base, "errors": [str(exc)]}),
            status_code=409,
        )
    return RedirectResponse(f"/runs/{rid}", status_code=303)


# ------------------------------------------------------------------------ JSON


@router.get("/api/runs/{rid}")
async def run_json(request: Request, rid: str) -> JSONResponse:
    db: Database = request.app.state.db
    run = await _require_run(db, rid)
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


@router.get("/api/runs/{rid}/amendments")
async def run_amendments(request: Request, rid: str) -> JSONResponse:
    db: Database = request.app.state.db
    await _require_run(db, rid)
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


@router.get("/api/runs/{rid}/spend")
async def run_spend(request: Request, rid: str) -> JSONResponse:
    db: Database = request.app.state.db
    run = await _require_run(db, rid)
    usage = await repo.list_token_usage_for_run(db, rid)
    return JSONResponse(
        {
            "cap": run.budget_cap_usd,
            "spend_usd": run.spend_usd,
            "projected_spend_usd": run.projected_spend_usd,
            "usage": usage,
        }
    )
