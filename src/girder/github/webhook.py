"""Inbound GitHub webhook receiver (improvements WS-05 / WP 9.1).

GitHub delivers ``issues.assigned``, ``issue_comment.created`` and
``pull_request_review_comment.created`` events to
``POST /api/webhooks/github``; this module verifies the ``X-Hub-Signature-256``
HMAC-SHA256 signature (the route checks it *before* any payload processing —
an invalid signature is a 403 and nothing is persisted), dispatches the
allowlisted events through :class:`WebhookProcessor`, and creates a project
run per event through the same ``repo.create_run`` → ``dispatch_generation``
pipeline the web console uses. Every other event is ignored with 200 (GitHub
treats non-2xx deliveries as failed webhooks and would disable them).

Injection seams for tests: ``resolve_project`` (GitHub full name → Girder
project; the default reads each project's git remote), ``create_run`` (the
run-creating pipeline; the default is repo.create_run + SPEC_PENDING
transition) and ``comment`` (the "Girder is working on this" issue comment;
the default posts via GitHubClient).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from girder.config import Secrets, Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run, RunStatus
from girder.fsm import transition_run
from girder.github.client import GitHubClient, parse_owner_repo
from girder.guard.redact import Redactor
from girder.util import run_host_cmd

log = logging.getLogger(__name__)

#: Events this receiver acts on; everything else is ignored with 200.
ALLOWED_EVENTS = frozenset({"issues", "issue_comment", "pull_request_review_comment"})

GIRDER_COMMAND = "/girder "

COMMENT_TEMPLATE = "Girder is working on this — [view run]({console_url}/runs/{run_id})"


def verify_github_signature(secret: str | None, body: bytes, header: str | None) -> bool:
    """HMAC-SHA256 check of the raw request body (``sha256=<hex>``).

    A missing secret, missing header, wrong prefix, or any digest mismatch is
    a rejection — the caller must not have touched the payload by then.
    """
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


@dataclass(slots=True)
class WebhookOutcome:
    """What one webhook delivery did (for the route's JSON and for tests)."""

    handled: bool
    reason: str
    run_id: str | None = None


ProjectResolver = Callable[[str], Awaitable[Project | None]]
RunFactory = Callable[[Project, str], Awaitable[Run]]
Commenter = Callable[[int, str, str], Awaitable[None]]  # (issue_number, repo_full_name, body)


async def _default_resolve_project(db: Database, full_name: str) -> Project | None:
    """Match ``owner/name`` against each registered project's git remote."""
    for project in await repo.list_projects(db):
        try:
            proc = await run_host_cmd(
                ["git", "-C", str(project.repo_path), "remote", "get-url", "origin"],
                timeout_s=10,
            )
            owner, name = parse_owner_repo(proc.stdout.strip())
        except Exception:
            continue  # a non-GitHub or missing remote is just a non-match
        if f"{owner}/{name}" == full_name:
            return project
    return None


async def _default_create_run(
    db: Database, settings: Settings, project: Project, intent: str
) -> Run:
    """The web console's run-creation pipeline, minus generation dispatch."""
    run = await repo.create_run(
        db, project.id, intent, branch="pending", budget_cap_usd=settings.budget.run_cap_usd
    )
    await repo.update_run_fields(db, run.id, branch=f"run/{run.id[:8]}")
    await transition_run(db, run.id, RunStatus.SPEC_PENDING, reason="github_webhook")
    return run


class WebhookProcessor:
    """Dispatch allowlisted GitHub webhook payloads to run creation."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        secrets: Secrets,
        *,
        resolve_project: ProjectResolver | None = None,
        create_run: RunFactory | None = None,
        comment: Commenter | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.secrets = secrets
        self._resolve_project = resolve_project or self._resolve_default
        self._create_run = create_run or self._create_run_default
        self._comment = comment or self._comment_default
        self._redactor = Redactor(secrets.redaction_secret_env_names)

    # -------------------------------------------------------------- dispatch

    async def handle(self, event: str, payload: dict[str, Any]) -> WebhookOutcome:
        """Route one verified delivery. Unknown events / non-matching payloads
        are ignored (``handled=False``), never errors."""
        if event not in ALLOWED_EVENTS:
            return WebhookOutcome(False, f"ignored event {event!r}")
        handler = {
            "issues": self._issues,
            "issue_comment": self._issue_comment,
            "pull_request_review_comment": self._review_comment,
        }[event]
        try:
            return await handler(payload)
        except Exception as exc:
            log.exception("webhook handler for %s failed", event)
            return WebhookOutcome(False, f"handler error: {exc}")

    # --------------------------------------------------------------- handlers

    async def _issues(self, payload: dict[str, Any]) -> WebhookOutcome:
        bot = self.settings.github.bot_account
        assignee = str((payload.get("assignee") or {}).get("login", ""))
        if payload.get("action") != "assigned" or not bot or assignee != bot:
            return WebhookOutcome(False, f"ignored assignment to {assignee or 'nobody'!r}")
        issue = _dict(payload, "issue")
        title = str(issue.get("title", "")).strip()
        body_text = str(issue.get("body") or "").strip()
        intent = f"{title}\n\n{body_text}".strip()
        return await self._start_run(payload, intent, issue_number=_issue_number(issue))

    async def _issue_comment(self, payload: dict[str, Any]) -> WebhookOutcome:
        comment = _dict(payload, "comment")
        text = str(comment.get("body", "")).strip()
        if not text.startswith(GIRDER_COMMAND):
            return WebhookOutcome(False, "comment does not invoke /girder")
        bot = self.settings.github.bot_account
        if bot and str(comment.get("user", {}).get("login", "")) == bot:
            return WebhookOutcome(False, "ignored the bot's own comment")
        issue = _dict(payload, "issue")
        intent = text[len(GIRDER_COMMAND) :].strip()
        return await self._start_run(payload, intent, issue_number=_issue_number(issue))

    async def _review_comment(self, payload: dict[str, Any]) -> WebhookOutcome:
        comment = _dict(payload, "comment")
        text = str(comment.get("body", "")).strip()
        if text != "/girder fix this":
            return WebhookOutcome(False, "review comment does not invoke /girder fix this")
        pr = _dict(payload, "pull_request")
        path = str(comment.get("path", "")).strip()
        intent = (
            f'On PR #{pr.get("number", "?")} ({pr.get("title", "")}):'
            f" fix this in `{path}`.\n\n{text}"
        )
        return await self._start_run(payload, intent, issue_number=int(pr.get("number", 0)))

    # ------------------------------------------------------------ shared tail

    async def _start_run(
        self, payload: dict[str, Any], intent: str, *, issue_number: int
    ) -> WebhookOutcome:
        if not intent:
            return WebhookOutcome(False, "empty intent")
        full_name = str(_dict(payload, "repository").get("full_name", ""))
        project = await self._resolve_project(full_name)
        if project is None:
            return WebhookOutcome(False, f"no Girder project tracks {full_name!r}")
        run = await self._create_run(project, intent)
        await repo.insert_event(
            self.db,
            "github_webhook_run_created",
            {"repository": full_name, "issue": issue_number, "run_id": run.id},
            run_id=run.id,
        )
        console_url = f"http://{self.settings.web.host}:{self.settings.web.port}"
        try:
            await self._comment(
                issue_number,
                full_name,
                COMMENT_TEMPLATE.format(console_url=console_url, run_id=run.id),
            )
        except Exception as exc:
            log.warning("webhook comment on %s#%s failed: %s", full_name, issue_number, exc)
        return WebhookOutcome(True, "run created", run_id=run.id)

    # ------------------------------------------------- default implementations

    async def _resolve_default(self, full_name: str) -> Project | None:
        return await _default_resolve_project(self.db, full_name)

    async def _create_run_default(self, project: Project, intent: str) -> Run:
        return await _default_create_run(self.db, self.settings, project, intent)

    async def _comment_default(self, issue_number: int, full_name: str, body: str) -> None:
        project = await _default_resolve_project(self.db, full_name)
        if project is None:
            return
        client = GitHubClient(
            self.settings,
            self.secrets,
            self._redactor,
            self.db,
            repo_path=Path(project.repo_path),
        )
        try:
            await client.comment_on_pr(issue_number, body)
        finally:
            await client.aclose()


def _dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _issue_number(issue: dict[str, Any]) -> int:
    try:
        return int(issue.get("number", 0))
    except (TypeError, ValueError):
        return 0


def parse_webhook_body(body: bytes) -> dict[str, Any]:
    """Decode a webhook JSON body; raises ``ValueError`` on garbage."""
    data = json.loads(body or b"{}")
    if not isinstance(data, dict):
        raise ValueError("webhook payload must be a JSON object")
    return data
