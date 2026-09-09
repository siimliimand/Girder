"""Telegram inbound channel for spec amendments (impl-plan §8.4, plan.md §8.4).

Amendments may be resolved "via Web UI or Telegram"; the Web UI path lives in
the API layer, this module is the Telegram half: a long-poll receiver that
pulls ``getUpdates`` from the Bot API, filters messages to the configured
``telegram_chat_id``, parses a small command grammar, and routes the decision
into :func:`girder.specs.amendment.resolve_amendment` (which owns the FSM
transitions, re-freeze, and notifications).

Command grammar (``<amendment-id>`` is the spec_amendments row id)::

    /approve <amendment-id>
    /reject  <amendment-id> <guidance...>
    /abort   <amendment-id>
    /help

Anything else is answered with usage help. Every accepted command is logged
as an ``amendment_telegram_command`` agent event for auditability (§8.6).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from girder.config import NotifyConfig, Secrets
from girder.db import repo
from girder.db.engine import Database
from girder.notify.notifier import TELEGRAM_API, Notifier

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S = 10.0

_COMMANDS = ("approve", "reject", "abort", "help")


@dataclass(slots=True)
class TelegramCommand:
    """Parsed ``/command`` from an inbound Telegram message."""

    command: str  # approve | reject | abort | help | unknown
    amendment_id: str | None
    guidance: str | None
    raw: str


def parse_telegram_command(text: str) -> TelegramCommand:
    """Parse the amendment command grammar; garbage maps to ``unknown``."""
    text = text.strip()
    if not text.startswith("/"):
        return TelegramCommand("unknown", None, None, text)
    parts = text.split(maxsplit=1)
    head = parts[0][1:]
    # tolerate "/approve@MyBot" — Telegram appends the bot username in groups
    head = head.split("@", maxsplit=1)[0]
    command = head.lower() if head.lower() in _COMMANDS else "unknown"
    rest = parts[1].strip() if len(parts) > 1 else ""
    if command in ("approve", "abort"):
        if not rest:
            return TelegramCommand("unknown", None, None, text)
        return TelegramCommand(command, rest.split()[0], None, text)
    if command == "reject":
        if not rest:
            return TelegramCommand("unknown", None, None, text)
        pieces = rest.split(maxsplit=1)
        amendment_id = pieces[0]
        guidance = pieces[1].strip() if len(pieces) > 1 else None
        return TelegramCommand("reject", amendment_id, guidance or None, text)
    if command == "help":
        return TelegramCommand("help", None, None, text)
    return TelegramCommand("unknown", None, None, text)


# resolver: (amendment_id, decision, guidance) -> human-readable outcome text
AmendmentResolver = Callable[[str, str, str | None], Awaitable[str]]

USAGE = (
    "Commands:\n"
    "/approve <amendment-id>\n"
    "/reject <amendment-id> <guidance...>\n"
    "/abort <amendment-id>"
)


class TelegramReceiver:
    """Long-poll ``getUpdates`` receiver routing amendment commands (§8.4).

    The HTTP transport is injectable for tests. Cancelling the ``run`` task
    stops the receiver gracefully; per-update exceptions are logged and never
    escape (a broken Telegram path must not take down the daemon).
    """

    def __init__(
        self,
        db: Database,
        config: NotifyConfig,
        secrets: Secrets,
        notifier: Notifier,
        resolver: AmendmentResolver,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self.db = db
        self.config = config
        self.secrets = secrets
        self.notifier = notifier
        self.resolver = resolver
        self.poll_interval_s = poll_interval_s
        self._offset = 0
        self._transport = transport
        self._timeout = poll_interval_s + 5.0

    # ------------------------------------------------------------- polling loop

    async def run(self) -> None:
        """Poll forever; cancel to stop. Errors are logged, never raised."""
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("telegram poll failed — retrying in %.0fs", self.poll_interval_s)
            await asyncio.sleep(self.poll_interval_s)

    async def poll_once(self) -> int:
        """Fetch and process pending updates once; returns updates handled."""
        token = self.secrets.notify_telegram_bot_token
        if not token:
            return 0
        if self._transport is not None:
            client = httpx.AsyncClient(transport=self._transport, timeout=self._timeout)
        else:
            client = httpx.AsyncClient(timeout=self._timeout)
        async with client:
            response = await client.post(
                f"{TELEGRAM_API}/bot{token}/getUpdates",
                json={"offset": self._offset, "timeout": 0},
            )
            response.raise_for_status()
            updates = response.json().get("result", [])
            handled = 0
            for update in updates:
                self._offset = max(self._offset, int(update["update_id"]) + 1)
                try:
                    await self._handle_update(update)
                except Exception:
                    log.exception("failed to handle telegram update %s", update.get("update_id"))
                handled += 1
            return handled

    # ------------------------------------------------------------- dispatch

    async def _handle_update(self, update: dict[str, object]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return
        # Chat-id filter (§8.4): commands are only honoured from the configured
        # operator chat; anything else is ignored (no reply, no event).
        if str(chat.get("id")) != str(self.config.telegram_chat_id):
            log.warning("ignoring telegram message from chat %s", chat.get("id"))
            return
        text = message.get("text")
        if not isinstance(text, str):
            return
        command = parse_telegram_command(text)
        await repo.insert_event(
            self.db,
            "amendment_telegram_command",
            {
                "command": command.command,
                "amendment_id": command.amendment_id,
                "chat_id": chat.get("id"),
            },
        )
        reply = await self._execute(command)
        await self.notifier.notify("info", "Telegram amendment command", reply)

    async def _execute(self, command: TelegramCommand) -> str:
        try:
            if command.command == "help":
                return USAGE
            if command.command == "unknown":
                return f"Unrecognised command.\n\n{USAGE}"
            if command.amendment_id is None:  # pragma: no cover — parse guarantees
                return USAGE
            outcome = await self.resolver(command.amendment_id, command.command, command.guidance)
            return f"{command.command}: {command.amendment_id}\n\n{outcome}"
        except Exception as exc:
            # Resolution failures (unknown id, wrong state, …) go back to the
            # operator instead of vanishing; the daemon itself is unaffected.
            log.info("telegram amendment command failed: %s", exc)
            return f"failed: {exc}"
