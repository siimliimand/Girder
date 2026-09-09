"""WP-1.8 — notifier: redaction before send/persist, failure tolerance.

Phase 0 exit criterion: a test alert containing a planted fake credential
arrives in the notification channel with the credential redacted.
"""

from __future__ import annotations

import json

import httpx
import pytest

from girder.config import NotifyConfig, Secrets
from girder.guard.redact import Redactor
from girder.notify.notifier import Notifier

PLANTED = "ghp_AAAABBBBCCCCDDDDEEEEFFFF"


class CaptureTransport(httpx.MockTransport):
    """Captures outgoing requests; fails any URL containing 'failing'."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        super().__init__(self._handler)

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "failing" in str(request.url):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def transport() -> CaptureTransport:
    return CaptureTransport()


@pytest.fixture
def secrets() -> Secrets:
    return Secrets(
        notify_telegram_bot_token="123:telegt",
        notify_discord_webhook_url="https://discord-failing.example/hook",
    )


def _notifier(transport: CaptureTransport, channels: list[str], db=None) -> Notifier:  # type: ignore[no-untyped-def]
    return Notifier(
        NotifyConfig(channels=channels, telegram_chat_id="42"),
        secrets=Secrets(notify_telegram_bot_token="123:telegt"),
        redactor=Redactor(),
        db=db,
        transport=transport,
    )


async def test_planted_credential_arrives_redacted(transport: CaptureTransport) -> None:
    notifier = _notifier(transport, ["telegram"])
    await notifier.notify(
        "critical",
        "Run failed",
        f"CI log line: token {PLANTED} rejected",
    )
    assert len(transport.requests) == 1
    sent = json.loads(transport.requests[0].content)
    assert PLANTED not in sent["text"]
    assert "ghp***[REDACTED:" in sent["text"]
    assert "CI log line:" in sent["text"]


async def test_db_row_is_redacted(transport: CaptureTransport, db) -> None:  # type: ignore[no-untyped-def]
    notifier = _notifier(transport, ["telegram"], db=db)
    await notifier.notify("warning", "Disk", f"secret {PLANTED} seen")
    rows = await db.fetchall("SELECT channel, payload_redacted, status FROM notifications_log")
    assert len(rows) == 1
    assert rows[0]["channel"] == "telegram"
    assert rows[0]["status"] == "sent"
    assert PLANTED not in rows[0]["payload_redacted"]
    assert "REDACTED" in rows[0]["payload_redacted"]


async def test_failed_channel_recorded_not_raised(transport: CaptureTransport) -> None:
    # discord webhook URL contains 'failing' -> transport returns 500
    notifier = Notifier(
        NotifyConfig(channels=["discord"]),
        secrets=Secrets(notify_discord_webhook_url="https://discord-failing.example/hook"),
        redactor=Redactor(),
        transport=transport,
    )
    results = await notifier.notify("error", "x", "body")
    assert results[0].status == "failed"
    assert "500" in (results[0].error or "") or "boom" in (results[0].error or "")


async def test_no_channels_still_audited(transport: CaptureTransport, db) -> None:  # type: ignore[no-untyped-def]
    notifier = _notifier(transport, [], db=db)
    results = await notifier.notify("info", "hello", "world")
    assert results[0].status == "logged"
    rows = await db.fetchall("SELECT * FROM notifications_log")
    assert len(rows) == 1


async def test_skipped_when_credential_missing(transport: CaptureTransport) -> None:
    notifier = Notifier(
        NotifyConfig(channels=["telegram"]),  # no chat_id configured
        secrets=Secrets(notify_telegram_bot_token="123:x"),
        redactor=Redactor(),
        transport=transport,
    )
    results = await notifier.notify("info", "t", "b")
    assert results[0].status == "skipped"
    assert transport.requests == []


async def test_multi_channel_delivery(transport: CaptureTransport) -> None:
    notifier = Notifier(
        NotifyConfig(channels=["telegram", "discord"], telegram_chat_id="42"),
        secrets=Secrets(
            notify_telegram_bot_token="123:t",
            notify_discord_webhook_url="https://discord.example/hook",
        ),
        redactor=Redactor(),
        transport=transport,
    )
    results = await notifier.notify("info", "both", "channels")
    assert [r.status for r in results] == ["sent", "sent"]
    assert len(transport.requests) == 2


async def test_telegram_payload_clamped_to_4096(transport: CaptureTransport) -> None:
    """An over-long escalation (recovery/integrity reports) is clamped to
    Telegram's 4096-char limit and the send is still attempted — it must not
    fail delivery with HTTP 400 (impl-plan §6.13)."""
    notifier = _notifier(transport, ["telegram"])
    results = await notifier.notify("critical", "recovery", "x" * 6000)
    assert results[0].status == "sent"
    assert len(transport.requests) == 1
    sent = json.loads(transport.requests[0].content)
    assert len(sent["text"]) <= 4096
    assert sent["text"].startswith("*[CRITICAL]*")
