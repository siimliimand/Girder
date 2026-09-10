"""Autonomy-tier views and overrides (WP 10.1).

Owns: ``GET /projects/{pid}/tier`` and ``POST /api/projects/{pid}/tier``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from girder.api.app import render
from girder.api.deps import Db
from girder.api.routes._shared import (
    merge_queue,
    merged_rows,
    require_project,
    review_window_state,
)
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project

router = APIRouter()


async def _tier_context(db: Database, request: Request, project: Project) -> dict[str, Any]:
    settings = request.app.state.settings
    queue = [
        q
        for q in await merge_queue(db)
        if q["project_id"] == project.id
    ]
    return {
        "project": project,
        "queue": queue,
        "streak": project.clean_merge_streak,
        "threshold": settings.autonomy.t2_required_streak,
        "merged": await merged_rows(db, project_id=project.id),
        "review_window": await review_window_state(db, settings, project),
    }


@router.get("/projects/{pid}/tier", response_class=HTMLResponse)
async def tier_page(request: Request, db: Db, pid: str) -> HTMLResponse:
    project = await require_project(db, pid)
    return render(request, "tier.html", await _tier_context(db, request, project))


@router.post("/api/projects/{pid}/tier", response_model=None)
async def set_tier(
    request: Request, db: Db, pid: str, tier: str = Form(...)
) -> HTMLResponse | RedirectResponse:
    """Autonomy-tier override (plan.md §2.3). Demotion is instant; promotion to
    T2 is gated on the project's clean-merge streak."""
    project = await require_project(db, pid)
    settings = request.app.state.settings
    try:
        value = int(tier)
    except ValueError:
        value = -1
    if value not in (0, 1, 2):
        return render(
            request,
            "tier.html",
            {**await _tier_context(db, request, project),
             "error": f"invalid tier {tier!r}: must be 0, 1 or 2."},
            status_code=400,
        )
    current = project.autonomy_tier
    if value > current and value == 2:
        required = settings.autonomy.t2_required_streak
        if project.clean_merge_streak < required:
            return render(
                request,
                "tier.html",
                {
                    **await _tier_context(db, request, project),
                    "error": (
                        f"cannot promote to T2: clean merge streak "
                        f"{project.clean_merge_streak} is below the required {required}."
                    ),
                },
                status_code=409,
            )
    await repo.set_project_tier(db, pid, value)
    return RedirectResponse(f"/projects/{pid}/tier", status_code=303)
