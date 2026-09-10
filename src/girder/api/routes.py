"""Routes for the approval web console (impl-plan §10).

SQL lives only in :mod:`girder.db`; everything here goes through repo fns and
:mod:`girder.fsm`. Generation runs as a tracked background task (see
:func:`girder.api.app.dispatch_generation`).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)

from girder.api.app import dispatch_generation, render
from girder.budget.guard import BudgetGuard
from girder.config import ModelRole, Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import (
    TERMINAL_RUN_STATUSES,
    Project,
    Run,
    RunStatus,
    SteeringKind,
    Task,
    TaskStatus,
)
from girder.fsm import transition_run
from girder.guard.redact import Redactor
from girder.specs.amendment import AmendmentError, resolve_amendment
from girder.specs.freeze import FreezeError, approve_and_freeze
from girder.specs.validator import OPENSPEC_TEMPLATE, SpecValidationError, parse_spec

router = APIRouter()


def _tier1_role(settings: Settings) -> ModelRole | None:
    """The tier1 ModelRole, or None when no roles are configured."""
    for cfg in settings.models.roles:
        if cfg.role == "tier1":
            return cfg
    return None


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


async def _run_context(
    db: Database, run_id: str, *, redactor: Redactor | None = None
) -> dict[str, object]:
    """Everything run.html and _run_panel.html need."""
    run = await _require_run(db, run_id)
    project = await _require_project(db, run.project_id)
    usage = await repo.list_token_usage_for_run(db, run_id)
    failure = await repo.get_latest_event(db, run_id, "spec_generation_failed")
    # Raw rejected model output from the latest failed generation, if the
    # failure carried one (redacted upstream by the gateway; nothing new here).
    failed_raw_output: dict[str, object] | None = None
    if failure is not None:
        raw = failure["payload"].get("raw_output")
        if raw:
            failed_raw_output = {
                "text": raw,
                "truncated": "[...truncated by girder at 20000 characters]" in raw,
            }
    spend = {
        "cap": run.budget_cap_usd,
        "spend_usd": run.spend_usd,
        "projected_spend_usd": run.projected_spend_usd,
    }
    pending_amendment = await repo.get_pending_amendment(db, run_id)
    roles = await repo.sum_token_usage_for_run(db, run_id)
    violations = await repo.list_integrity_violations_for_run(db, run_id)
    # Diff viewer (§10): stored diffs were redacted at insert time, but
    # historical views re-run through the SAME redactor as the live path.
    diffs_by_task: dict[str, str] = {}
    for d in await repo.list_attempt_diffs_for_run(db, run_id):
        if d.task_id is None:
            continue
        text = d.diff_redacted if redactor is None else _redact(redactor, d.diff_redacted)
        if text is not None:
            diffs_by_task.setdefault(d.task_id, text)
    return {
        "run": run,
        "project": project,
        "spend": spend,
        "usage": usage,
        "roles": roles,
        "violations": violations,
        "generation_error": failure,
        "failed_raw_output": failed_raw_output,
        "pending_amendment": pending_amendment,
        "proposal_tasks": _proposal_task_rows(run.proposal_md),
        "diffs_by_task": diffs_by_task,
    }


def _estimate_ctx(app: Any, run: Run, project: Project) -> dict[str, object]:
    """Pre-approval cost estimate for the review panel (plan.md Phase 1)."""
    settings: Settings = app.state.settings
    budget: BudgetGuard = app.state.budget
    return {
        "estimate_next_generation": _next_generation_estimate(
            settings, budget, run, Path(project.repo_path)
        )
    }


def _proposal_task_rows(proposal: str | None) -> list[dict[str, Any]]:
    """File-impact rows for the review UI (plan.md Phase 1): id, title, type,
    scope_globs and success criteria of every task in the proposal's
    frontmatter. Best-effort — an unparsable proposal yields no rows (the raw
    proposal is still shown verbatim below the table)."""
    if proposal is None:
        return []
    try:
        doc = parse_spec(proposal)
    except SpecValidationError:
        return []
    return [
        {
            "id": t.id,
            "title": t.title,
            "type": t.type.value,
            "scope_globs": t.scope_globs,
            "criteria": " · ".join(t.success_criteria),
        }
        for t in doc.tasks
    ]


def _next_generation_prompt_chars(run: Run, repo_path: Path) -> int:
    """Character count of the REAL next tier1 generation prompt.

    Mirrors the SpecGenerator assembly (specs/generator.py): system framing
    (untrusted-content rule + OpenSpec template) plus the user turn
    (<user-intent> + untrusted README / file-listing blocks). Generator
    internals are reused read-only — that file is owned by another workstream,
    so we do not edit it; no model call is dispatched. Regenerate feedback is
    not yet known at estimate time and is omitted (slight underestimate only).
    """
    from girder.specs import generator as spec_generator

    system = spec_generator._SYSTEM_TEMPLATE.format(
        untrusted_rule=spec_generator._UNTRUSTED_RULE,
        template=OPENSPEC_TEMPLATE,
    )
    parts = [f"<user-intent>\n{run.intent}\n</user-intent>"]
    readme = spec_generator._read_readme(repo_path)
    if readme is not None:
        parts.append(f'<untrusted-data source="README.md">\n{readme}\n</untrusted-data>')
    listing = spec_generator._file_listing(repo_path)
    if listing is not None:
        parts.append(f'<untrusted-data source="file-listing">\n{listing}\n</untrusted-data>')
    return len(system) + len("\n\n".join(parts))


def _next_generation_estimate(
    settings: Settings, budget: BudgetGuard, run: Run, repo_path: Path
) -> str:
    """Best-effort USD estimate for the next tier1 generation call on *run*.

    Sized from the real next-call prompt assembly (system template + README +
    intent — NOT the stored proposal, which does not exist on first review or
    after Regenerate, exactly when the call is imminent). Feeds the same
    BudgetGuard.preflight the gateway runs before dispatching; renders ``n/a``
    when no tier1 role is configured instead of failing.
    """
    role = _tier1_role(settings)
    if role is None:
        return "n/a"
    decision = budget.preflight(role, _next_generation_prompt_chars(run, repo_path), run)
    return f"${decision.est_cost_usd:.4f}"


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
    review_window = await _review_window_state(db, request.app.state.settings, project)
    return render(
        request,
        "project.html",
        {"project": project, "runs": runs, "review_window": review_window},
    )


def _review_window_message(window: int) -> str:
    return (
        f"T1 review window exceeded — mark recent merges reviewed first "
        f"(window: {window} unreviewed merges)."
    )


async def _review_window_state(
    db: Database, settings: Settings, project: Project
) -> dict[str, object]:
    """Unreviewed-merge count vs the project's T1 review window (issue 20)."""
    unreviewed = await repo.count_unreviewed_merges(db, project.id)
    window = settings.autonomy.t1_review_window
    return {
        "unreviewed": unreviewed,
        "window": window,
        "exceeded": project.autonomy_tier == 1 and unreviewed >= window,
    }


@router.post("/api/projects/{pid}/runs", response_model=None)
async def create_run(
    request: Request, pid: str, intent: str = Form(...)
) -> RedirectResponse | HTMLResponse:
    app = request.app
    db: Database = app.state.db
    project = await _require_project(db, pid)
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
    window_state = await _review_window_state(db, settings, project)
    if window_state["exceeded"]:
        return render(
            request,
            "project.html",
            {
                "project": project,
                "runs": await repo.list_runs_for_project(db, pid),
                "error": _review_window_message(settings.autonomy.t1_review_window),
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


@router.get("/runs/{rid}", response_class=HTMLResponse)
async def run_page(request: Request, rid: str) -> HTMLResponse:
    db: Database = request.app.state.db
    ctx = await _run_context(db, rid, redactor=request.app.state.redactor)
    run: Run = ctx["run"]  # type: ignore[assignment]
    ctx["graph"] = await _graph(db, run)
    ctx.update(_estimate_ctx(request.app, run, ctx["project"]))  # type: ignore[arg-type]
    ctx = _panel_context(run, ctx)
    return render(request, "run.html", ctx)


@router.get("/runs/{rid}/panel", response_class=HTMLResponse)
async def run_panel(request: Request, rid: str) -> HTMLResponse:
    db: Database = request.app.state.db
    base = await _run_context(db, rid, redactor=request.app.state.redactor)
    base.update(_estimate_ctx(request.app, base["run"], base["project"]))  # type: ignore[arg-type]
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
    if run.spec_hash is not None:
        # Frozen run: the committed spec is immutable — refuse to edit the
        # proposal so the review UI can never diverge from the frozen spec.
        return render(
            request,
            "_run_panel.html",
            _panel_context(run, {**base, "errors": ["cannot edit: spec is frozen"]}),
            status_code=409,
        )
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
    request: Request, rid: str, aid: str, scope_globs: str = Form("")
) -> HTMLResponse | RedirectResponse:
    # §8.4: an approval may widen the amended task's write scope. Globs arrive
    # newline/comma-separated; blank input means "no scope change".
    raw = [g.strip() for chunk in scope_globs.splitlines() for g in chunk.split(",")]
    globs = [g for g in raw if g] or None
    return await _resolve_amendment_route(request, rid, aid, "approved", scope_globs=globs)


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
    scope_globs: list[str] | None = None,
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
            scope_globs=scope_globs,
            notifier=request.app.state.notifier,
        )
    except AmendmentError as exc:
        return render(
            request,
            "_run_panel.html",
            _panel_context(run, {**base, "errors": [str(exc)]}),
            status_code=409,
        )
    # Actions taken from the §10 Amendments inbox bounce back to their Referer
    # (the inbox); run-page actions have no Referer and stay on the run page.
    return RedirectResponse(request.headers.get("Referer") or f"/runs/{rid}", status_code=303)


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
async def projects_json(request: Request) -> JSONResponse:
    db: Database = request.app.state.db
    projects = await repo.list_projects(db)
    return JSONResponse(
        [_project_json(p, await _project_timestamps(db, p.id)) for p in projects]
    )


@router.get("/api/projects/{pid}")
async def project_json(request: Request, pid: str) -> JSONResponse:
    db: Database = request.app.state.db
    project = await _require_project(db, pid)
    return JSONResponse(_project_json(project, await _project_timestamps(db, pid)))


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


@router.get("/api/runs/{rid}/diffs")
async def run_diffs(request: Request, rid: str) -> JSONResponse:
    """Persisted attempt diffs of a run (§10 diff viewer), re-redacted through
    the same redactor as the live event stream (plan §10)."""
    app = request.app
    db: Database = app.state.db
    redactor: Redactor = app.state.redactor
    await _require_run(db, rid)
    diffs = await repo.list_attempt_diffs_for_run(db, rid)
    return JSONResponse(
        {
            "diffs": [
                {
                    "attempt_id": d.attempt_id,
                    "task_id": d.task_id,
                    "base_commit": d.base_commit,
                    "head_commit": d.head_commit,
                    "diff": _redact(redactor, d.diff_redacted),
                    "created_at": d.created_at,
                }
                for d in diffs
            ]
        }
    )


@router.get("/api/runs/{rid}/spend")
async def run_spend(request: Request, rid: str) -> JSONResponse:
    db: Database = request.app.state.db
    run = await _require_run(db, rid)
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


def _redact_value(redactor: Redactor, value: Any) -> Any:
    """Re-redact every string in a decoded JSON payload (plan §10: historical
    views run through the same redactor as the live write path)."""
    if isinstance(value, str):
        return redactor.redact(value)[0]
    if isinstance(value, dict):
        return {k: _redact_value(redactor, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(redactor, v) for v in value]
    return value


def _redact(redactor: Redactor, text: str | None) -> str | None:
    return None if text is None else redactor.redact(text)[0]


@router.get("/api/runs/{rid}/events")
async def run_events(
    request: Request,
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
    app = request.app
    db: Database = app.state.db
    await _require_run(db, rid)
    poll = max(poll_s, 0.05)
    redactor: Redactor = app.state.redactor

    async def gen() -> AsyncIterator[str]:
        last = after_id
        try:
            for _ in range(max(1, max_ticks)):
                rows = await repo.list_events_for_run(db, rid, after_id=last)
                for row in rows:
                    last = int(row["id"])
                    payload = _redact_value(redactor, json.loads(row["payload_json"]))
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
async def run_graph(request: Request, rid: str) -> JSONResponse:
    db: Database = request.app.state.db
    run = await _require_run(db, rid)
    return JSONResponse(await _graph(db, run))


# -------------------------------------------------------------- steering (6.2)


@router.post("/api/runs/{rid}/steer", response_model=None)
async def steer(
    request: Request,
    rid: str,
    action: str = Form(...),
    directive: str = Form(""),
    task_id: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    """Queue a steering event for the orchestrator pump (plan.md §3.5).

    Pause and inject act at the build-phase task boundary (consumed by the
    orchestrator pump between attempts); abort is observed at task boundaries.
    """
    db: Database = request.app.state.db
    base = await _run_context(db, rid)
    run: Run = base["run"]  # type: ignore[assignment]

    def fail(msg: str, status: int = 400) -> HTMLResponse:
        return render(
            request,
            "_run_panel.html",
            _panel_context(run, {**base, "errors": [msg]}),
            status_code=status,
        )

    try:
        kind = SteeringKind(action)
    except ValueError:
        return fail(f"unknown steering action {action!r}.")

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


# ------------------------------------------- merge queue / tiers / history (6.4)


async def _merge_queue(db: Database) -> list[dict[str, Any]]:
    projects = {p.id: p for p in await repo.list_projects(db)}
    runs = await repo.list_runs_in_status(db, "merge_pending_human")
    return [
        {
            "run_id": r.id,
            "project_id": r.project_id,
            "project_name": (
                projects[r.project_id].name if r.project_id in projects else r.project_id
            ),
            "branch": r.branch,
            "pr_number": r.pr_number,
            "budget_cap_usd": r.budget_cap_usd,
            "spend_usd": r.spend_usd,
        }
        for r in runs
    ]


@router.get("/api/merge-queue")
async def merge_queue(request: Request) -> JSONResponse:
    db: Database = request.app.state.db
    return JSONResponse({"queue": await _merge_queue(db)})


async def _merged_rows(db: Database, *, project_id: str | None = None) -> list[dict[str, Any]]:
    """Merged runs with their review-window state (issue 3: every merged run
    needs a human ``merge_reviewed`` event to clear the T1 window)."""
    projects = {p.id: p for p in await repo.list_projects(db)}
    runs = await repo.list_runs_in_status(db, "merged", project_id=project_id)
    return [
        {
            "run_id": r.id,
            "project_id": r.project_id,
            "project_name": (
                projects[r.project_id].name if r.project_id in projects else r.project_id
            ),
            "branch": r.branch,
            "pr_number": r.pr_number,
            "reviewed": await repo.get_latest_event(db, r.id, "merge_reviewed") is not None,
        }
        for r in runs
    ]


@router.get("/merge-queue", response_class=HTMLResponse)
async def merge_queue_page(request: Request) -> HTMLResponse:
    db: Database = request.app.state.db
    projects = await repo.list_projects(db)
    unreviewed = {p.id: await repo.count_unreviewed_merges(db, p.id) for p in projects}
    return render(
        request,
        "merge_queue.html",
        {
            "queue": await _merge_queue(db),
            "projects": projects,
            "merged": await _merged_rows(db),
            "unreviewed": unreviewed,
        },
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
                "intent": _excerpt(r.intent, 80) if r else None,
                "task_id": a.task_id,
                "reason": a.reason,
                "suggested_change": a.suggested_change,
            }
        )
    return rows


@router.get("/amendments", response_class=HTMLResponse)
async def amendments_page(request: Request) -> HTMLResponse:
    db: Database = request.app.state.db
    return render(request, "amendments.html", {"pending": await _amendments_inbox(db)})


@router.post("/api/runs/{rid}/reviewed", response_model=None)
async def mark_merge_reviewed(request: Request, rid: str) -> HTMLResponse | RedirectResponse:
    """Produce the ``merge_reviewed`` event the T1 review window counts
    (issue 3) — the exact event type repo.count_unreviewed_merges checks."""
    db: Database = request.app.state.db
    run = await _require_run(db, rid)
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
async def merge_run(request: Request, rid: str) -> HTMLResponse | RedirectResponse:
    """Human merge click for a T0 ``merge_pending_human`` run (issue 21).

    Re-checks the cheaply checkable merge gates read-only (fresh integrity
    ledger, PR mergeable), then calls the existing GitHubClient.merge_pr. NO
    run-state transition happens here: post-merge bookkeeping (sync main,
    archive, streak, prune) belongs to the delivery pump, whose
    ``_pump_merge_pending`` detects the merged PR out-of-band and performs the
    MERGED transition itself.
    """
    db: Database = request.app.state.db
    run = await _require_run(db, rid)
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
    project = await _require_project(db, run.project_id)
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


async def _tier_context(db: Database, request: Request, project: Project) -> dict[str, Any]:
    settings = request.app.state.settings
    queue = [
        q
        for q in await _merge_queue(db)
        if q["project_id"] == project.id
    ]
    return {
        "project": project,
        "queue": queue,
        "streak": project.clean_merge_streak,
        "threshold": settings.autonomy.t2_required_streak,
        "merged": await _merged_rows(db, project_id=project.id),
        "review_window": await _review_window_state(db, settings, project),
    }


@router.get("/projects/{pid}/tier", response_class=HTMLResponse)
async def tier_page(request: Request, pid: str) -> HTMLResponse:
    db: Database = request.app.state.db
    project = await _require_project(db, pid)
    return render(request, "tier.html", await _tier_context(db, request, project))


@router.post("/api/projects/{pid}/tier", response_model=None)
async def set_tier(
    request: Request, pid: str, tier: str = Form(...)
) -> HTMLResponse | RedirectResponse:
    """Autonomy-tier override (plan.md §2.3). Demotion is instant; promotion to
    T2 is gated on the project's clean-merge streak."""
    db: Database = request.app.state.db
    project = await _require_project(db, pid)
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


# -------------------------------------------------------- history & post-mortem


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
async def history(request: Request) -> JSONResponse:
    db: Database = request.app.state.db
    return JSONResponse({"runs": await _history(db)})


@router.get("/history", response_class=HTMLResponse)
async def history_page(request: Request) -> HTMLResponse:
    db: Database = request.app.state.db
    return render(request, "history.html", {"runs": await _history(db)})


def _excerpt(text: str | None, limit: int = 200) -> str | None:
    if text is None:
        return None
    return text[:limit] + ("…" if len(text) > limit else "")


@router.get("/runs/{rid}/postmortem", response_class=HTMLResponse)
async def postmortem(request: Request, rid: str) -> HTMLResponse:
    """Historical post-mortem view (WP 6.5). Every dynamic string is re-run
    through the redactor before rendering (plan §10)."""
    app = request.app
    db: Database = app.state.db
    redactor: Redactor = app.state.redactor
    run = await _require_run(db, rid)
    project = await _require_project(db, run.project_id)
    events = await repo.list_events_for_run(db, rid, after_id=0, limit=100000)
    # Diff viewer (§10): stored diffs re-redacted through the same redactor as
    # the live path (plan §10).
    diff_by_attempt = {
        d.attempt_id: _redact(redactor, d.diff_redacted) or ""
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
                    "input_json": _redact(redactor, c["input_json"]),
                    "output_excerpt": _redact(
                        redactor, _excerpt(c["output_blob_redacted"])
                    ),
                }
                for c in await repo.list_tool_calls_for_attempt(db, a.id)
            ]
            prompts = [
                {
                    "turn": p.turn,
                    "role": p.role,
                    "content": _redact(redactor, p.content_redacted),
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
                            "payload": _redact_value(
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
            "detail": _redact_value(redactor, json.loads(v["detail_json"])),
        }
        for v in await repo.list_integrity_violations_for_run(db, rid)
    ]
    notifications = [
        {**n, "payload_redacted": _redact(redactor, n["payload_redacted"])}
        for n in await repo.list_notifications_for_run(db, rid)
    ]
    amendments = [
        {
            "id": a.id,
            "reason": _redact(redactor, a.reason),
            "suggested_change": _redact(redactor, a.suggested_change),
            "status": a.status,
            "guidance": _redact(redactor, a.guidance),
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
