"""Notification engine (plan.md Phase 0 task 7 / impl-plan §6.13).

Every payload passes through the redaction pipeline *before* it is sent or
persisted — nothing reaches a channel or ``notifications_log`` unredacted.
Delivery failures are recorded, never raised: a notification outage must not
take down the orchestrator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from girder.config import NotifyConfig, Secrets
from girder.db import repo
from girder.db.engine import Database
from girder.guard.redact import Redactor

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
DISCORD_MAX = 2000
TELEGRAM_MAX = 4096


@dataclass
class ChannelResult:
    channel: str
    status: str  # sent | failed | skipped
    error: str | None = None


class Notifier:
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
        """Render, redact, deliver, and audit one notification."""
        payload = f"*[{level.upper()}]* {title}\n\n{body}"
        payload, _ = self.redactor.redact(payload)

        results: list[ChannelResult]
        if not self.config.channels:
            results = [ChannelResult(channel="log", status="logged")]
        else:
            results = []
            if self._transport is not None:
                client = httpx.AsyncClient(transport=self._transport, timeout=10.0)
            else:
                client = httpx.AsyncClient(timeout=10.0)
            async with client:
                for channel in self.config.channels:
                    results.append(await self._deliver(client, channel, payload))

        if self.db is not None:
            for r in results:
                await repo.insert_notification(
                    self.db, r.channel, payload, r.status, run_id=run_id
                )
        for r in results:
            if r.status == "failed":
                log.error("notification via %s failed: %s", r.channel, r.error)
        return results

    async def _deliver(
        self, client: httpx.AsyncClient, channel: str, payload: str
    ) -> ChannelResult:
        try:
            if channel == "telegram":
                token = self.secrets.notify_telegram_bot_token
                chat_id = self.config.telegram_chat_id
                if not token or not chat_id:
                    return ChannelResult(channel, "skipped", "missing token or chat_id")
                response = await client.post(
                    f"{TELEGRAM_API}/bot{token}/sendMessage",
                    json={
                        "chat_id": chat_id,
                        "text": payload[:TELEGRAM_MAX],
                        "parse_mode": "Markdown",
                    },
                )
                response.raise_for_status()
                return ChannelResult(channel, "sent")
            if channel == "discord":
                url = self.secrets.notify_discord_webhook_url
                if not url:
                    return ChannelResult(channel, "skipped", "missing webhook url")
                response = await client.post(url, json={"content": payload[:DISCORD_MAX]})
                response.raise_for_status()
                return ChannelResult(channel, "sent")
            return ChannelResult(channel, "skipped", f"unknown channel {channel!r}")
        except httpx.HTTPError as exc:
            return ChannelResult(channel, "failed", str(exc))
