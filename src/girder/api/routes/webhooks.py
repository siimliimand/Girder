"""Automation webhooks (WS-05 / WP 9.1 + WP 9.2).

Owns: ``POST /api/webhooks/github`` (inbound GitHub App webhook receiver)
and ``POST /api/webhooks/slack`` (slash commands + interactive payloads).

Both endpoints authenticate via HMAC instead of the console's session
model; WS-10's API-key middleware should exempt these paths (webhooks
authenticate via their own signatures).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from girder.api.app import dispatch_generation
from girder.api.routes._shared import review_window_message, review_window_state
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, RunStatus, TERMINAL_RUN_STATUSES
from girder.fsm import transition_run
from girder.github.webhook import WebhookProcessor, parse_webhook_body, verify_github_signature
from girder.config import Settings
from girder.notify.slack import (
    make_amendment_resolver,
    parse_interactive_payload,
    parse_slash_command,
    verify_slack_signature,
)

router = APIRouter()


def _webhook_processor(app: Any) -> WebhookProcessor:
    """Webhook processor for the GitHub route. Tests inject a factory via
    ``app.state.webhook_processor_factory`` (mirrors ``github_factory``)."""
    factory = getattr(app.state, "webhook_processor_factory", None)
    if factory is not None:
        processor: WebhookProcessor = factory()
        return processor
    return WebhookProcessor(app.state.db, app.state.settings, app.state.secrets)


@router.post("/api/webhooks/github")
async def github_webhook(request: Request) -> JSONResponse:
    """GitHub App webhook receiver (WP 9.1).

    The ``X-Hub-Signature-256`` HMAC-SHA256 check runs against the raw body
    BEFORE any payload processing — an invalid signature is a 403 and nothing
    is persisted. Unknown events are ignored with 200 (GitHub treats non-2xx
    deliveries as failed and would eventually disable the webhook).
    """
    app = request.app
    settings: Settings = app.state.settings
    if not settings.github.webhook_enabled:
        raise HTTPException(status_code=404, detail="github webhooks are disabled")
    body = await request.body()
    secret = app.state.secrets.github_webhook_secret
    if not verify_github_signature(secret, body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=403, detail="invalid signature")
    try:
        payload = parse_webhook_body(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    event = request.headers.get("X-GitHub-Event", "")
    outcome = await _webhook_processor(app).handle(event, payload)
    if outcome.run_id is not None:
        dispatch_generation(app, outcome.run_id, None)
    return JSONResponse(
        {"ok": True, "handled": outcome.handled, "reason": outcome.reason, "run_id": outcome.run_id}
    )


_SLACK_USAGE = (
    "Usage: `/girder run [project=<name>] <intent>` · `/girder status` · `/girder help`"
)


def _slack_response(text: str, *, in_channel: bool = False) -> JSONResponse:
    return JSONResponse(
        {"response_type": "in_channel" if in_channel else "ephemeral", "text": text}
    )


def _slack_form(body: bytes) -> dict[str, str]:
    from urllib.parse import parse_qs

    return {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items() if v}


async def _slack_active_runs_text(db: Database) -> str:
    """Human-readable list of non-terminal runs for ``/girder status``."""
    lines: list[str] = []
    projects = {p.id: p.name for p in await repo.list_projects(db)}
    for status in sorted(s.value for s in RunStatus if s not in TERMINAL_RUN_STATUSES):
        for run in await repo.list_runs_in_status(db, status):
            name = projects.get(run.project_id, run.project_id)
            lines.append(f"• `{run.id[:8]}` {status} — {name}: {run.intent[:60]}")
    return "\n".join(lines) if lines else "No active runs."


async def _slack_create_run(
    app: Any, project: Project, intent: str
) -> str:
    """Create a run from a Slack slash command; returns the response text."""
    db: Database = app.state.db
    settings: Settings = app.state.settings
    window = await review_window_state(db, settings, project)
    if window["exceeded"]:
        return review_window_message(settings.autonomy.t1_review_window)
    run = await repo.create_run(
        db, project.id, intent, branch="pending", budget_cap_usd=settings.budget.run_cap_usd
    )
    await repo.update_run_fields(db, run.id, branch=f"run/{run.id[:8]}")
    await transition_run(db, run.id, RunStatus.SPEC_PENDING, reason="slack_slash_command")
    await repo.insert_event(db, "slack_run_created", {"run_id": run.id}, run_id=run.id)
    dispatch_generation(app, run.id, None)
    console = f"http://{settings.web.host}:{settings.web.port}"
    return f"Run created: `{run.id[:8]}` — [view run]({console}/runs/{run.id})"


async def _slack_project(app: Any, name: str | None) -> Project | str:
    """Resolve the slash command's project (``project=<name>`` or the only
    registered project); a str return is an error message for the user."""
    db: Database = app.state.db
    if name:
        project = await repo.get_project_by_name(db, name)
        if project is None:
            return f"no project named {name!r} is registered."
        return project
    projects = await repo.list_projects(db)
    if len(projects) == 1:
        return projects[0]
    if not projects:
        return "no projects are registered yet."
    return (
        "multiple projects registered — pick one with "
        "`/girder run project=<name> <intent>`: "
        + ", ".join(sorted(p.name for p in projects))
    )


@router.post("/api/webhooks/slack")
async def slack_webhook(request: Request) -> JSONResponse:
    """Slack slash-command + interactive-payload receiver (WP 9.2).

    Signature verification (``v0`` scheme, replay-protected) runs on the raw
    body before any parsing. Enabled only when ``notify_slack_signing_secret``
    is configured.
    """
    app = request.app
    db: Database = app.state.db
    signing_secret = app.state.secrets.notify_slack_signing_secret
    if not signing_secret:
        raise HTTPException(status_code=404, detail="slack webhooks are disabled")
    body = await request.body()
    if not verify_slack_signature(
        signing_secret,
        body,
        request.headers.get("X-Slack-Request-Timestamp"),
        request.headers.get("X-Slack-Signature"),
    ):
        raise HTTPException(status_code=403, detail="invalid signature")
    form = _slack_form(body)

    interactive = form.get("payload")
    if interactive:
        action = parse_interactive_payload(interactive)
        if action is None or action.decision == "unknown" or action.amendment_id is None:
            return _slack_response("Unrecognised interactive payload.")
        from girder.specs.amendment import AmendmentError

        notifier = getattr(app.state, "notifier", None)
        resolver = make_amendment_resolver(db, notifier)
        try:
            outcome = await resolver(action.amendment_id, action.decision, None)
        except AmendmentError as exc:
            return _slack_response(f"failed: {exc}")
        return _slack_response(f"{action.decision}: {action.amendment_id}\n\n{outcome}")

    if form.get("command", "").lower() != "/girder":
        return _slack_response(f"Unknown command {form.get('command', '')!r}. {_SLACK_USAGE}")
    parsed = parse_slash_command(form.get("text", ""))
    if parsed.action in ("help", "unknown"):
        return _slack_response(_SLACK_USAGE)
    if parsed.action == "status":
        return _slack_response(await _slack_active_runs_text(db))
    # action == "run"
    if parsed.intent is None:
        return _slack_response("Usage: `/girder run [project=<name>] <intent>`")
    resolved = await _slack_project(app, parsed.project)
    if isinstance(resolved, str):
        return _slack_response(resolved)
    return _slack_response(await _slack_create_run(app, resolved, parsed.intent), in_channel=True)
