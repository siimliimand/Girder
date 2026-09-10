"""Steering and human merge/review actions (WP 10.1).

Owns: ``POST /api/runs/{rid}/steer``, ``POST /api/runs/{rid}/reviewed``,
``POST /api/runs/{rid}/merge``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from girder.api.app import render
from girder.api.deps import Db
from girder.api.routes._shared import (
    panel_context,
    require_project,
    require_run,
    run_context,
)
from girder.db import repo
from girder.db.models import (
    TERMINAL_RUN_STATUSES,
    Project,
    Run,
    RunStatus,
    SteeringKind,
)

router = APIRouter()


@router.post("/api/runs/{rid}/steer", response_model=None)
async def steer(
    request: Request,
    db: Db,
    rid: str,
    action: str = Form(...),
    directive: str = Form(""),
    task_id: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    """Queue a steering event for the orchestrator pump (plan.md §3.5).

    Pause and inject act at the build-phase task boundary (consumed by the
    orchestrator pump between attempts); abort is observed at task boundaries.
    """
    base = await run_context(db, rid)
    run: Run = base["run"]  # type: ignore[assignment]

    def fail(msg: str, status: int = 400) -> HTMLResponse:
        return render(
            request,
            "_run_panel.html",
            panel_context(run, {**base, "errors": [msg]}),
            status_code=status,
        )

    try:
        kind = SteeringKind(action)
    except ValueError:
        return fail(f"unknown steering action {action!r}.")

    # R14: abort is consumable from any status by design ("queues an abort in
    # any status"). Every other kind is only ever read at the ACTIVE pump
    # boundary (or mid-attempt by the agent runtime), while an escalated run's
    # pump reads abort only (_pump_escalated) and terminal runs are never
    # pumped again. Accepting other kinds there would write a queue entry
    # nothing ever consumes — a silent no-op presented as a successful action.
    if kind is not SteeringKind.ABORT and (
        run.status is RunStatus.ESCALATED or run.status in TERMINAL_RUN_STATUSES
    ):
        return fail(
            f"run is {run.status.value}: {kind.value!r} would queue a steering"
            " event no pump ever reads. Only abort applies to this status.",
            status=409,
        )

    payload: dict[str, Any] = {}
    if kind is SteeringKind.INJECT:
        if not directive.strip():
            return fail("inject requires a non-blank directive.")
        payload = {"directive": directive.strip()}
    elif kind in (SteeringKind.SKIP, SteeringKind.FORCE_PASS):
        tasks = {t.id for t in await repo.list_tasks_for_run(db, rid)}
        if task_id not in tasks:
            return fail("task_id must name a task belonging to this run.")
        payload = {"task_id": task_id}

    await repo.insert_steering_event(db, rid, kind.value, payload)
    await repo.insert_event(db, "steering_requested", {"kind": kind.value, **payload}, run_id=rid)
    return RedirectResponse(f"/runs/{rid}", status_code=303)


@router.post("/api/runs/{rid}/reviewed", response_model=None)
async def mark_merge_reviewed(
    request: Request, db: Db, rid: str
) -> HTMLResponse | RedirectResponse:
    """Produce the ``merge_reviewed`` event the T1 review window counts
    (issue 3) — the exact event type repo.count_unreviewed_merges checks."""
    run = await require_run(db, rid)
    if run.status != RunStatus.MERGED:
        raise HTTPException(
            status_code=409,
            detail=f"cannot mark reviewed: run status is {run.status.value}, not merged",
        )
    await repo.insert_event(db, "merge_reviewed", {"run_id": rid}, run_id=rid)
    return RedirectResponse(request.headers.get("Referer") or "/merge-queue", status_code=303)


def _github_for(app: Any, project: Project) -> Any:
    """GitHub client for the merge endpoint. Tests inject a fake via
    ``app.state.github_factory``; production builds the real client per call."""
    factory = getattr(app.state, "github_factory", None)
    if factory is not None:
        return factory(project)
    from girder.github.client import GitHubClient  # lazy: keeps routes import-light

    return GitHubClient(
        app.state.settings,
        app.state.secrets,
        app.state.redactor,
        app.state.db,
        repo_path=Path(project.repo_path),
    )


@router.post("/api/runs/{rid}/merge", response_model=None)
async def merge_run(request: Request, db: Db, rid: str) -> HTMLResponse | RedirectResponse:
    """Human merge click for a T0 ``merge_pending_human`` run (issue 21).

    Re-checks the cheaply checkable merge gates read-only (fresh integrity
    ledger, PR mergeable), then calls the existing GitHubClient.merge_pr. NO
    run-state transition happens here: post-merge bookkeeping (sync main,
    archive, streak, prune) belongs to the delivery pump, whose
    ``_pump_merge_pending`` detects the merged PR out-of-band and performs the
    MERGED transition itself.
    """
    run = await require_run(db, rid)
    if run.status != RunStatus.MERGE_PENDING_HUMAN:
        raise HTTPException(
            status_code=409,
            detail=(
                f"cannot merge: run status is {run.status.value}, not merge_pending_human"
            ),
        )
    fresh = await repo.get_run(db, rid)
    if fresh is None:  # pragma: no cover - _require_run already loaded it
        raise HTTPException(status_code=404, detail="run not found")
    if fresh.integrity_violations > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"merge gate: {fresh.integrity_violations} integrity violation(s) — "
                "merge blocked"
            ),
        )
    if fresh.pr_number is None:
        raise HTTPException(status_code=409, detail="merge gate: run has no open PR")
    project = await require_project(db, run.project_id)
    client = _github_for(request.app, project)
    own_client = getattr(request.app.state, "github_factory", None) is None
    try:
        if not await client.pr_is_mergeable(fresh.pr_number):
            raise HTTPException(
                status_code=409, detail=f"merge gate: PR #{fresh.pr_number} is not mergeable"
            )
        outcome = await client.merge_pr(
            fresh.pr_number,
            commit_title=f"{fresh.branch}: {fresh.intent.strip()[:60]}",
        )
    finally:
        if own_client:
            await client.aclose()
    if not outcome.merged:
        raise HTTPException(
            status_code=409,
            detail=f"GitHub merge refused for PR #{fresh.pr_number}: {outcome.reason}",
        )
    return RedirectResponse("/merge-queue", status_code=303)
