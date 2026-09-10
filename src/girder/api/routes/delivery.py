"""Merge queue views (WP 10.1).

Owns: ``GET /api/merge-queue`` and the ``GET /merge-queue`` page. (The
``/api/runs/{rid}/reviewed|merge`` actions live in :mod:`steering` — they act
on a run, while this module renders the delivery queue.)
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from girder.api.app import render
from girder.api.deps import Db
from girder.api.routes._shared import load_merge_queue, merged_rows
from girder.db import repo

router = APIRouter()


@router.get("/api/merge-queue")
async def merge_queue_json(request: Request, db: Db) -> JSONResponse:
    return JSONResponse({"queue": await load_merge_queue(db)})


@router.get("/merge-queue", response_class=HTMLResponse)
async def merge_queue_page(request: Request, db: Db) -> HTMLResponse:
    projects = await repo.list_projects(db)
    unreviewed = {p.id: await repo.count_unreviewed_merges(db, p.id) for p in projects}
    return render(
        request,
        "merge_queue.html",
        {
            "queue": await load_merge_queue(db),
            "projects": projects,
            "merged": await merged_rows(db),
            "unreviewed": unreviewed,
        },
    )
