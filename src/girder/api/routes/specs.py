"""Spec review actions and cost estimation (WP 10.1).

Owns: ``POST /api/runs/{rid}/approve|edit|regenerate`` plus the pre-approval
cost-estimate helpers the run page/panel use.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from girder.api.app import dispatch_generation, render
from girder.api.deps import Db
from girder.api.routes._shared import panel_context, run_context
from girder.budget.guard import BudgetGuard
from girder.config import ModelRole, Settings
from girder.db import repo
from girder.db.models import Project, Run, RunStatus
from girder.fsm import transition_run
from girder.specs.freeze import FreezeError, approve_and_freeze
from girder.specs.validator import OPENSPEC_TEMPLATE, SpecValidationError, parse_spec

router = APIRouter()


def _tier1_role(settings: Settings) -> ModelRole | None:
    """The tier1 ModelRole, or None when no roles are configured."""
    for cfg in settings.models.roles:
        if cfg.role == "tier1":
            return cfg
    return None


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


def _estimate_ctx(app: Any, run: Run, project: Project) -> dict[str, object]:
    """Pre-approval cost estimate for the review panel (plan.md Phase 1)."""
    settings: Settings = app.state.settings
    budget: BudgetGuard = app.state.budget
    return {
        "estimate_next_generation": _next_generation_estimate(
            settings, budget, run, Path(project.repo_path)
        )
    }


@router.post("/api/runs/{rid}/approve", response_model=None)
async def approve(request: Request, db: Db, rid: str) -> HTMLResponse | RedirectResponse:
    base = await run_context(db, rid)
    run: Run = base["run"]  # type: ignore[assignment]
    project: Project = base["project"]  # type: ignore[assignment]
    if run.spec_hash is not None:
        return render(
            request,
            "_run_panel.html",
            panel_context(run, {**base, "errors": ["run is already frozen"]}),
            status_code=400,
        )
    if run.status != RunStatus.SPEC_PENDING or not run.proposal_md:
        return render(
            request,
            "_run_panel.html",
            panel_context(
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
            panel_context(run, {**base, "errors": exc.errors}),
            status_code=400,
        )
    except FreezeError as exc:
        return render(
            request,
            "_run_panel.html",
            panel_context(run, {**base, "errors": [str(exc)]}),
            status_code=400,
        )
    return RedirectResponse(f"/runs/{rid}", status_code=303)


@router.post("/api/runs/{rid}/edit", response_model=None)
async def edit(
    request: Request, db: Db, rid: str, proposal: str = Form(...)
) -> HTMLResponse | RedirectResponse:
    base = await run_context(db, rid)
    run: Run = base["run"]  # type: ignore[assignment]
    if run.spec_hash is not None:
        # Frozen run: the committed spec is immutable — refuse to edit the
        # proposal so the review UI can never diverge from the frozen spec.
        return render(
            request,
            "_run_panel.html",
            panel_context(run, {**base, "errors": ["cannot edit: spec is frozen"]}),
            status_code=409,
        )
    try:
        parse_spec(proposal)
    except SpecValidationError as exc:
        return render(
            request,
            "_run_panel.html",
            panel_context(run, {**base, "errors": exc.errors}),
            status_code=400,
        )
    await repo.update_run_fields(db, rid, proposal_md=proposal)
    return RedirectResponse(f"/runs/{rid}", status_code=303)


@router.post("/api/runs/{rid}/regenerate", response_model=None)
async def regenerate(
    request: Request, db: Db, rid: str, feedback: str = Form("")
) -> HTMLResponse | RedirectResponse:
    app = request.app
    base = await run_context(db, rid)
    run: Run = base["run"]  # type: ignore[assignment]
    if run.status != RunStatus.SPEC_PENDING:
        return render(
            request,
            "_run_panel.html",
            panel_context(
                run, {**base, "errors": [f"cannot regenerate: status is {run.status.value}"]}
            ),
            status_code=400,
        )
    await repo.update_run_fields(db, rid, proposal_md=None)
    await transition_run(db, rid, RunStatus.SPEC_PENDING, reason="regenerate")
    dispatch_generation(app, rid, feedback.strip() or None)
    return RedirectResponse(f"/runs/{rid}", status_code=303)
