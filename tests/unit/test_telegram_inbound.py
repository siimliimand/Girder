"""Telegram amendment inbound (impl-plan §8.4): parsing, chat filter, routing."""

from __future__ import annotations

import json
from typing import Any

import httpx

from girder.config import NotifyConfig, Secrets
from girder.db import repo
from girder.notify.telegram_inbound import (
    TelegramReceiver,
    parse_telegram_command,
)

CHAT_ID = "4242"


# --------------------------------------------------------------------- parsing


def test_parse_approve() -> None:
    cmd = parse_telegram_command("/approve abc-123")
    assert cmd.command == "approve"
    assert cmd.amendment_id == "abc-123"
    assert cmd.guidance is None


def test_parse_reject_with_guidance() -> None:
    cmd = parse_telegram_command("/reject abc-123 keep the API but rename X")
    assert cmd.command == "reject"
    assert cmd.amendment_id == "abc-123"
    assert cmd.guidance == "keep the API but rename X"


def test_parse_reject_without_guidance() -> None:
    cmd = parse_telegram_command("/reject abc-123")
    assert cmd.command == "reject"
    assert cmd.guidance is None


def test_parse_abort() -> None:
    cmd = parse_telegram_command("/abort abc-123")
    assert cmd.command == "abort"
    assert cmd.amendment_id == "abc-123"


def test_parse_help_and_garbage() -> None:
    assert parse_telegram_command("/help").command == "help"
    assert parse_telegram_command("/frobnicate x").command == "unknown"
    assert parse_telegram_command("hello there").command == "unknown"
    assert parse_telegram_command("/approve").command == "unknown"  # missing id


def test_parse_tolerates_bot_suffix() -> None:
    cmd = parse_telegram_command("/approve@MyGirderBot abc-123")
    assert cmd.command == "approve"
    assert cmd.amendment_id == "abc-123"


# ------------------------------------------------------------------- receiver


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def notify(self, level: str, title: str, body: str, **kw: Any) -> list[Any]:
        self.sent.append(body)
        return []


def _transport(updates: list[dict[str, Any]]) -> tuple[
    httpx.MockTransport, list[dict[str, Any]]
]:
    """getUpdates transport that hands out *updates* once, then nothing."""
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        batch, updates[:] = updates[:], []
        return httpx.Response(200, json={"ok": True, "result": batch})

    return httpx.MockTransport(handler), requests


def _receiver(
    db: Any,
    updates: list[dict[str, Any]],
    resolver: Any,
    notifier: FakeNotifier,
    *,
    chat_id: str = CHAT_ID,
) -> tuple[TelegramReceiver, list[dict[str, Any]]]:
    transport, requests = _transport(updates)
    receiver = TelegramReceiver(
        db=db,
        config=NotifyConfig(channels=["telegram"], telegram_chat_id=chat_id),
        secrets=Secrets(notify_telegram_bot_token="123:tok"),
        notifier=notifier,  # type: ignore[arg-type]
        resolver=resolver,
        transport=transport,
    )
    return receiver, requests


def _update(update_id: int, chat_id: str, text: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id}, "text": text},
    }


async def test_chat_id_filter_drops_foreign_messages(db) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, str | None]] = []

    async def resolver(aid: str, decision: str, guidance: str | None) -> str:
        calls.append((aid, decision, guidance))
        return "ok"

    notifier = FakeNotifier()
    receiver, _ = _receiver(
        db,
        [_update(1, "999", "/approve x"), _update(2, CHAT_ID, "/help")],
        resolver,
        notifier,
    )
    assert await receiver.poll_once() == 2
    assert calls == []  # foreign chat ignored, /help needs no resolver
    assert len(notifier.sent) == 1  # the in-chat /help got a usage reply
    assert "Commands" in notifier.sent[0]


async def test_commands_route_into_resolver(db) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, str | None]] = []

    async def resolver(aid: str, decision: str, guidance: str | None) -> str:
        calls.append((aid, decision, guidance))
        return f"resolved {decision}"

    notifier = FakeNotifier()
    receiver, _ = _receiver(
        db,
        [
            _update(1, CHAT_ID, "/approve am1"),
            _update(2, CHAT_ID, "/reject am2 do it differently please"),
            _update(3, CHAT_ID, "/abort am3"),
        ],
        resolver,
        notifier,
    )
    assert await receiver.poll_once() == 3
    assert calls == [
        ("am1", "approve", None),
        ("am2", "reject", "do it differently please"),
        ("am3", "abort", None),
    ]
    assert len(notifier.sent) == 3
    assert all("resolved" in s for s in notifier.sent)


async def test_commands_logged_as_events(db) -> None:  # type: ignore[no-untyped-def]
    async def resolver(aid: str, decision: str, guidance: str | None) -> str:
        return "ok"

    receiver, _ = _receiver(db, [_update(7, CHAT_ID, "/approve am7")], resolver, FakeNotifier())
    await receiver.poll_once()
    rows = await db.fetchall(
        "SELECT event_type, payload_json FROM agent_events"
        " WHERE event_type = 'amendment_telegram_command'"
    )
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload == {"command": "approve", "amendment_id": "am7", "chat_id": CHAT_ID}


async def test_offset_tracking(db) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, str | None]] = []

    async def resolver(aid: str, decision: str, guidance: str | None) -> str:
        calls.append((aid, decision, guidance))
        return "ok"

    receiver, requests = _receiver(
        db,
        [_update(10, CHAT_ID, "/approve a"), _update(11, CHAT_ID, "/abort b")],
        resolver,
        FakeNotifier(),
    )
    assert await receiver.poll_once() == 2
    assert receiver._offset == 12  # highest update_id + 1
    assert requests[0]["offset"] == 0
    # second poll confirms the confirmed offset is sent back to Telegram
    await receiver.poll_once()
    assert requests[1]["offset"] == 12


async def test_resolver_failure_replies_not_raises(db) -> None:  # type: ignore[no-untyped-def]
    async def resolver(aid: str, decision: str, guidance: str | None) -> str:
        raise KeyError("boom")

    notifier = FakeNotifier()
    receiver, _ = _receiver(db, [_update(1, CHAT_ID, "/approve nope")], resolver, notifier)
    assert await receiver.poll_once() == 1
    assert "failed" in notifier.sent[0]


async def test_no_token_polls_nothing() -> None:
    async def resolver(aid: str, decision: str, guidance: str | None) -> str:
        return "ok"

    transport, requests = _transport([_update(1, CHAT_ID, "/approve a")])
    receiver = TelegramReceiver(
        db=None,  # type: ignore[arg-end]
        config=NotifyConfig(channels=["telegram"], telegram_chat_id=CHAT_ID),
        secrets=Secrets(),  # no token
        notifier=FakeNotifier(),  # type: ignore[arg-type]
        resolver=resolver,
        transport=transport,
    )
    assert await receiver.poll_once() == 0
    assert requests == []


def test_insert_event_signature_unchanged() -> None:
    # guard the repo API the receiver depends on (kind, payload, run_id kwarg)
    import inspect

    sig = inspect.signature(repo.insert_event)
    assert list(sig.parameters) == ["db", "event_type", "payload", "run_id", "attempt_id"]
