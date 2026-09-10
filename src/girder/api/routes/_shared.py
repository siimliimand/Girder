"""Internal helpers shared by the console route modules (WP 10.1).

No routes are registered here; this is the non-DI half of what used to live at
the top of the old monolithic ``routes.py``. Public-ish names are re-exported
from :mod:`girder.api.routes` for backward compatibility.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from girder.config import Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run, RunStatus
from girder.guard.redact import Redactor
from girder.specs.validator import SpecValidationError, parse_spec


async def require_run(db: Database, run_id: str) -> Run:
    run = await repo.get_run(db, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    return run


async def require_project(db: Database, project_id: str) -> Project:
    project = await repo.get_project(db, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def redact(redactor: Redactor, text: str | None) -> str | None:
    return None if text is None else redactor.redact(text)[0]


def redact_value(redactor: Redactor, value: Any) -> Any:
    """Re-redact every string in a decoded JSON payload (plan §10: historical
    views run through the same redactor as the live write path)."""
    if isinstance(value, str):
        return redactor.redact(value)[0]
    if isinstance(value, dict):
        return {k: redact_value(redactor, v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(redactor, v) for v in value]
    return value


def excerpt(text: str | None, limit: int = 200) -> str | None:
    if text is None:
        return None
    return text[:limit] + ("…" if len(text) > limit else "")


def proposal_task_rows(proposal: str | None) -> list[dict[str, Any]]:
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


async def run_context(
    db: Database, run_id: str, *, redactor_: Redactor | None = None
) -> dict[str, object]:
    """Everything run.html and _run_panel.html need."""
    run = await require_run(db, run_id)
    project = await require_project(db, run.project_id)
    usage = await repo.list_token_usage_for_run(db, run_id)
    failure = await repo.get_latest_event(db, run_id, "spec_generation_failed")
    # A successful generation supersedes the failure it followed: after a
    # regenerate the stale banner/artifact must not keep implying the current
    # proposal is broken. Only the LATEST outcome may claim the panel.
    finished = await repo.get_latest_event(db, run_id, "spec_generation_finished")
    if failure is not None and finished is not None and finished["id"] > failure["id"]:
        failure = None
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
        text = d.diff_redacted if redactor_ is None else redact(redactor_, d.diff_redacted)
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
        "proposal_tasks": proposal_task_rows(run.proposal_md),
        "diffs_by_task": diffs_by_task,
    }


def panel_context(run: Run, base: dict[str, object]) -> dict[str, object]:
    ctx = dict(base)
    poll = (
        run.status == RunStatus.SPEC_PENDING
        and run.proposal_md is None
        and ctx.get("generation_error") is None
    )
    ctx["poll"] = poll
    return ctx


def review_window_message(window: int) -> str:
    return (
        f"T1 review window exceeded — mark recent merges reviewed first "
        f"(window: {window} unreviewed merges)."
    )


async def review_window_state(
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


async def load_merge_queue(db: Database) -> list[dict[str, Any]]:
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


async def merged_rows(db: Database, *, project_id: str | None = None) -> list[dict[str, Any]]:
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
