"""Project pages, project creation, and run creation (WP 10.1).

Owns: ``GET /`` (index), ``POST /api/projects``, ``GET /projects/{pid}``,
``POST /api/projects/{pid}/runs``, ``GET /api/projects``,
``GET /api/projects/{pid}``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from girder.api.deps import Db
from girder.api.routes._shared import (
    require_project,
    review_window_message,
    review_window_state,
)
from girder.api.app import dispatch_generation, render
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, RunStatus
from girder.fsm import transition_run

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Db) -> HTMLResponse:
    projects = await repo.list_projects(db)
    return render(request, "index.html", {"projects": projects})


@router.post("/api/projects", response_model=None)
async def create_project(
    request: Request, db: Db, name: str = Form(...), repo_path: str = Form(...)
) -> HTMLResponse | RedirectResponse:
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
async def project_page(request: Request, db: Db, pid: str) -> HTMLResponse:
    project = await require_project(db, pid)
    runs = await repo.list_runs_for_project(db, pid)
    review_window = await review_window_state(db, request.app.state.settings, project)
    return render(
        request,
        "project.html",
        {"project": project, "runs": runs, "review_window": review_window},
    )


@router.post("/api/projects/{pid}/runs", response_model=None)
async def create_run(
    request: Request, db: Db, pid: str, intent: str = Form(...)
) -> RedirectResponse | HTMLResponse:
    app = request.app
    project = await require_project(db, pid)
    if not intent.strip():
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
    # Issue 20: the T1 review window gates NEW runs at this boundary — a T1
    # project with the window full must get its merges reviewed before more
    # autonomous work is generated (the scheduler itself is untouched).
    window_state = await review_window_state(db, settings, project)
    if window_state["exceeded"]:
        return render(
            request,
            "project.html",
            {
                "project": project,
                "runs": await repo.list_runs_for_project(db, pid),
                "error": review_window_message(settings.autonomy.t1_review_window),
                "review_window": window_state,
            },
            status_code=409,
        )
    run = await repo.create_run(
        db, pid, intent, branch="pending", budget_cap_usd=settings.budget.run_cap_usd
    )
    await repo.update_run_fields(db, run.id, branch=f"run/{run.id[:8]}")
    await transition_run(db, run.id, RunStatus.SPEC_PENDING, reason="spec_generation_dispatched")
    dispatch_generation(app, run.id, None)
    return RedirectResponse(f"/runs/{run.id}", status_code=303)


# ------------------------------------------------------------------------ JSON


def _project_json(project: Project, timestamps: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": project.id,
        "name": project.name,
        "repo_path": project.repo_path,
        "autonomy_tier": project.autonomy_tier,
        "clean_merge_streak": project.clean_merge_streak,
        "created_at": (timestamps or {}).get("created_at"),
        "updated_at": (timestamps or {}).get("updated_at"),
    }


async def _project_timestamps(db: Database, project_id: str) -> dict[str, Any]:
    # created_at/updated_at live in the projects table but not on the Project
    # dataclass (girder.db is another workstream) — read them directly here.
    row = await db.fetchone(
        "SELECT created_at, updated_at FROM projects WHERE id = ?", (project_id,)
    )
    return dict(row) if row is not None else {}


@router.get("/api/projects")
async def projects_json(request: Request, db: Db) -> JSONResponse:
    projects = await repo.list_projects(db)
    return JSONResponse(
        [_project_json(p, await _project_timestamps(db, p.id)) for p in projects]
    )


@router.get("/api/projects/{pid}")
async def project_json(request: Request, db: Db, pid: str) -> JSONResponse:
    project = await require_project(db, pid)
    return JSONResponse(_project_json(project, await _project_timestamps(db, pid)))
