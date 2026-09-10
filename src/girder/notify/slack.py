"""Slack integration (improvements WS-05 / WP 9.2).

Two halves:

* :class:`SlackNotifier` — outbound notifications through ``chat.postMessage``
  with the same interface as :class:`girder.notify.notifier.Notifier`
  (redact-before-send, ``ChannelResult`` accounting, ``notifications_log``
  audit rows, delivery failures recorded never raised).
* inbound support for ``POST /api/webhooks/slack`` — request signature
  verification (``X-Slack-Signature``, the ``v0`` scheme, with replay
  protection) plus parsing of the ``/girder`` slash command and Block Kit
  interactive payloads. The route lives in :mod:`girder.api.routes`; the
  amendment resolver it hands interactive Approve/Reject actions to is built
  by :func:`make_amendment_resolver` (same shape the ``girder pump`` daemon
  builds for the Telegram receiver).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from girder.config import NotifyConfig, Secrets
from girder.db import repo
from girder.db.engine import Database
from girder.guard.redact import Redactor
from girder.notify.notifier import ChannelResult

log = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"
SLACK_MAX = 4000  # chat.postMessage text cap (40k, kept conservatively bounded)

#: Slack's documented replay window for signed requests.
MAX_AGE_S = 300

GIRDER_SLASH = "/girder"


def verify_slack_signature(
    signing_secret: str | None,
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    *,
    now: float | None = None,
) -> bool:
    """Slack ``v0`` request signing check with replay protection.

    The signed basestring is ``v0:{timestamp}:{body}``; a missing secret or
    header, a stale timestamp, or any digest mismatch is a rejection.
    """
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    current = now if now is not None else time.time()
    if abs(current - ts) > MAX_AGE_S:
        return False
    base = f"v0:{timestamp}:".encode() + body
    expected = hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"v0={expected}", signature)


@dataclass(slots=True)
class SlashCommand:
    """Parsed ``/girder`` slash-command text."""

    action: str  # run | status | help | unknown
    project: str | None  # optional project=<name> selector for "run"
    intent: str | None
    raw: str


def parse_slash_command(text: str) -> SlashCommand:
    """Parse the ``/girder`` command grammar::

        /girder run [project=<name>] <intent...>
        /girder status
        /girder help

    Anything else maps to ``unknown``.
    """
    raw = text.strip()
    body = raw[len(GIRDER_SLASH) :].strip() if raw.startswith(GIRDER_SLASH) else raw
    parts = body.split(maxsplit=1)
    action = parts[0].lower() if parts else ""
    rest = parts[1].strip() if len(parts) > 1 else ""
    if action == "status":
        return SlashCommand("status", None, None, raw)
    if action == "help":
        return SlashCommand("help", None, None, raw)
    if action == "run":
        project: str | None = None
        if rest.startswith("project="):
            chunks = rest.split(maxsplit=1)
            project = chunks[0][len("project=") :].strip() or None
            rest = chunks[1].strip() if len(chunks) > 1 else ""
        if not rest:
            return SlashCommand("run", project, None, raw)
        return SlashCommand("run", project, rest, raw)
    return SlashCommand("unknown", None, None, raw)


@dataclass(slots=True)
class InteractiveAction:
    """One Approve/Reject/Abort decision from a Block Kit interactive payload."""

    decision: str  # approve | reject | abort | unknown
    amendment_id: str | None


def parse_interactive_payload(payload_json: str | bytes) -> InteractiveAction | None:
    """Extract the amendment decision from an interactive message payload.

    Expected shape (``callback_id: girder_amendment``)::

        {"callback_id": "girder_amendment",
         "actions": [{"name": "decision", "value": "approve:<amendment_id>"}]}

    Anything else returns ``None`` (ignored).
    """
    try:
        data = json.loads(payload_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("callback_id") != "girder_amendment":
        return None
    actions = data.get("actions")
    if not isinstance(actions, list) or not actions:
        return None
    first = actions[0]
    if not isinstance(first, dict):
        return None
    value = str(first.get("value", ""))
    decision, _, amendment_id = value.partition(":")
    decision = decision.strip().lower()
    if decision not in ("approve", "reject", "abort"):
        return InteractiveAction("unknown", None)
    return InteractiveAction(decision, amendment_id.strip() or None)


class SlackNotifier:
    """Outbound Slack deliveries — same interface as ``Notifier.notify``."""

    def __init__(
        self,
        config: NotifyConfig,
        secrets: Secrets,
        redactor: Redactor,
        *,
        db: Database | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.secrets = secrets
        self.redactor = redactor
        self.db = db
        self._transport = transport

    async def notify(
        self, level: str, title: str, body: str, *, run_id: str | None = None
    ) -> list[ChannelResult]:
        """Redact, deliver to the configured Slack channel, and audit."""
        payload = f"*[{level.upper()}]* {title}\n\n{body}"
        payload, _ = self.redactor.redact(payload)
        token = self.secrets.notify_slack_bot_token
        channel = self.config.slack_channel
        if not token or not channel:
            result = ChannelResult("slack", "skipped", "missing token or slack_channel")
        else:
            result = await self._post(token, channel, payload)
        if self.db is not None:
            await repo.insert_notification(self.db, "slack", payload, result.status, run_id=run_id)
        if result.status == "failed":
            log.error("notification via slack failed: %s", result.error)
        return [result]

    async def _post(self, token: str, channel: str, text: str) -> ChannelResult:
        try:
            if self._transport is not None:
                client = httpx.AsyncClient(transport=self._transport, timeout=10.0)
            else:
                client = httpx.AsyncClient(timeout=10.0)
            async with client:
                response = await client.post(
                    f"{SLACK_API}/chat.postMessage",
                    json={"channel": channel, "text": text[:SLACK_MAX]},
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                data = response.json()
                if not data.get("ok", False):
                    return ChannelResult("slack", "failed", str(data.get("error", "unknown")))
                return ChannelResult("slack", "sent")
        except httpx.HTTPError as exc:
            return ChannelResult("slack", "failed", str(exc))


def make_amendment_resolver(db: Database, notifier: Any) -> Any:
    """The amendment resolver handed to the Slack route for interactive
    Approve/Reject payloads — the same closure ``girder pump`` builds for the
    Telegram receiver (cli.py ``_start_telegram_receiver``)."""
    from girder.specs.amendment import AmendmentError, resolve_amendment

    async def resolver(amendment_id: str, decision: str, guidance: str | None) -> str:
        amendment = await repo.get_spec_amendment(db, amendment_id)
        if amendment is None:
            raise AmendmentError(f"amendment {amendment_id} not found")
        run = await repo.get_run(db, amendment.run_id)
        if run is None:
            raise AmendmentError(f"run {amendment.run_id} not found")
        project = await repo.get_project(db, run.project_id)
        if project is None:
            raise AmendmentError(f"project {run.project_id} not found")
        outcome = await resolve_amendment(
            db,
            project=project,
            run=run,
            amendment=amendment,
            decision=decision,
            guidance=guidance,
            notifier=notifier,
        )
        return (
            f"amendment resolved: {outcome.decision}"
            f" (run {run.id} -> {outcome.run_status}, task -> {outcome.task_status})"
        )

    return resolver
