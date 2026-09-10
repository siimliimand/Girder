"""Unit tests for the Slack integration (WS-05 / WP 9.2): signature
verification, slash-command parsing, interactive payload parsing, the
SlackNotifier delivery path, and the /api/webhooks/slack route."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import httpx

from girder.api.app import create_app
from girder.config import NotifyConfig, Secrets, Settings
from girder.db import repo
from girder.db.engine import Database, default_migrations_dir
from girder.notify.slack import (
    SlackNotifier,
    parse_interactive_payload,
    parse_slash_command,
    verify_slack_signature,
)

SECRET = "signing-secret-unit"


def sign(body: bytes, ts: str, secret: str = SECRET) -> str:
    base = f"v0:{ts}:".encode() + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ signature


def test_signature_valid() -> None:
    body = b"text=hello"
    ts = str(int(time.time()))
    assert verify_slack_signature(SECRET, body, ts, sign(body, ts))


def test_signature_tampered_rejected() -> None:
    body = b"text=hello"
    ts = str(int(time.time()))
    assert not verify_slack_signature(SECRET, b"text=hacked", ts, sign(body, ts))


def test_signature_stale_timestamp_rejected() -> None:
    body = b"text=hello"
    old = str(int(time.time()) - 3600)
    assert not verify_slack_signature(SECRET, body, old, sign(body, old))
    assert not verify_slack_signature(SECRET, body, "not-a-number", sign(body, "0"))


def test_signature_missing_parts_rejected() -> None:
    assert not verify_slack_signature(None, b"x", "1", "v0=abc")
    assert not verify_slack_signature(SECRET, b"x", None, "v0=abc")
    assert not verify_slack_signature(SECRET, b"x", "1", None)


# ------------------------------------------------------------------- parsing


def test_parse_run_with_intent() -> None:
    cmd = parse_slash_command('run fix the login bug')
    assert cmd.action == "run"
    assert cmd.project is None
    assert cmd.intent == "fix the login bug"


def test_parse_run_with_project_selector() -> None:
    cmd = parse_slash_command("run project=acme refactor config")
    assert cmd.action == "run"
    assert cmd.project == "acme"
    assert cmd.intent == "refactor config"


def test_parse_run_without_intent_is_incomplete() -> None:
    cmd = parse_slash_command("run")
    assert cmd.action == "run" and cmd.intent is None


def test_parse_status_and_help() -> None:
    assert parse_slash_command("status").action == "status"
    assert parse_slash_command("help").action == "help"


def test_parse_unknown() -> None:
    assert parse_slash_command("delete everything").action == "unknown"
    assert parse_slash_command("").action == "unknown"


def test_parse_interactive_payload() -> None:
    payload = json.dumps(
        {
            "callback_id": "girder_amendment",
            "actions": [{"name": "decision", "value": "approve:amend-1"}],
        }
    )
    action = parse_interactive_payload(payload)
    assert action is not None
    assert action.decision == "approve"
    assert action.amendment_id == "amend-1"


def test_parse_interactive_garbage_returns_none() -> None:
    assert parse_interactive_payload("not json") is None
    assert parse_interactive_payload(json.dumps({"callback_id": "other"})) is None


# ------------------------------------------------------------------- notifier


async def test_notifier_sends_and_audits(db: Database) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    notifier = SlackNotifier(
        NotifyConfig(channels=["slack"], slack_channel="C123"),
        Secrets(notify_slack_bot_token="xoxb-test"),
        _redactor(),
        db=db,
        transport=transport,
    )
    results = await notifier.notify("info", "PR opened", "run r1: PR #9", run_id="r1")
    assert results[0].status == "sent"
    body = json.loads(calls[0].content)
    assert body["channel"] == "C123"
    assert "[INFO]" in body["text"]
    row = await db.fetchone(
        "SELECT channel, status FROM notifications_log ORDER BY id DESC LIMIT 1"
    )
    assert row is not None and row["channel"] == "slack" and row["status"] == "sent"


async def test_notifier_missing_token_skipped(db: Database) -> None:
    notifier = SlackNotifier(
        NotifyConfig(channels=["slack"], slack_channel="C123"), Secrets(), _redactor(), db=db
    )
    results = await notifier.notify("info", "t", "b")
    assert results[0].status == "skipped"


async def test_notifier_slack_error_is_failed_not_raised(db: Database) -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
    )
    notifier = SlackNotifier(
        NotifyConfig(channels=["slack"], slack_channel="C0"),
        Secrets(notify_slack_bot_token="xoxb-test"),
        _redactor(),
        transport=transport,
    )
    results = await notifier.notify("error", "t", "b")
    assert results[0].status == "failed"
    assert results[0].error == "channel_not_found"


def _redactor() -> object:
    from girder.guard.redact import Redactor

    return Redactor()


# --------------------------------------------------------------------- route


def _form(body: dict[str, str]) -> bytes:
    return urlencode(body).encode()


async def test_route_slash_run_creates_run(tmp_path: object) -> None:
    settings = Settings()
    secrets = Secrets(notify_slack_signing_secret=SECRET)
    app = create_app(
        db_path=tmp_path / "s.db",  # type: ignore[union-attr]
        settings=settings,
        secrets=secrets,
        migrations_dir=default_migrations_dir(),
        generator_factory=lambda: _NoopGen(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client, app.router.lifespan_context(app):
        # register one project so the single-project shortcut applies
        await client.post("/api/projects", data={"name": "acme", "repo_path": str(tmp_path)})
        body = _form({"command": "/girder", "text": "run ship the feature"})
        headers = {
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "X-Slack-Signature": sign(body, str(int(time.time()))),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        resp = await client.post("/api/webhooks/slack", content=body, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["response_type"] == "in_channel"
        assert "Run created" in resp.json()["text"]

        bad = await client.post(
            "/api/webhooks/slack",
            content=body,
            headers={
                "X-Slack-Request-Timestamp": str(int(time.time())),
                "X-Slack-Signature": sign(body, str(int(time.time())), "other"),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        assert bad.status_code == 403

        status_body = _form({"command": "/girder", "text": "status"})
        status = await client.post(
            "/api/webhooks/slack",
            content=status_body,
            headers={
                "X-Slack-Request-Timestamp": str(int(time.time())),
                "X-Slack-Signature": sign(status_body, str(int(time.time()))),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        assert status.status_code == 200
        assert "spec_pending" in status.json()["text"]

        while app.state.background_tasks:
            import asyncio

            await asyncio.gather(*list(app.state.background_tasks))


async def test_route_disabled_without_signing_secret(tmp_path: object) -> None:
    app = create_app(
        db_path=tmp_path / "s.db",  # type: ignore[union-attr]
        settings=Settings(),
        secrets=Secrets(),
        migrations_dir=default_migrations_dir(),
        generator_factory=lambda: _NoopGen(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client, app.router.lifespan_context(app):
        resp = await client.post("/api/webhooks/slack", content=b"command=%2Fgirder")
        assert resp.status_code == 404


class _NoopGen:
    async def generate(self, **kwargs: object) -> str:  # pragma: no cover
        return "---\nschema: girder.openspec/v1\ntitle: t\nintent: i\ntasks: []\n---\nx"


async def test_run_from_slash_command_lands_in_db(tmp_path: object) -> None:
    """The slack-created run uses the standard SPEC_PENDING pipeline."""
    settings = Settings()
    secrets = Secrets(notify_slack_signing_secret=SECRET)
    app = create_app(
        db_path=tmp_path / "s.db",  # type: ignore[union-attr]
        settings=settings,
        secrets=secrets,
        migrations_dir=default_migrations_dir(),
        generator_factory=lambda: _NoopGen(),
    )
    db: Database = None  # type: ignore[assignment]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client, app.router.lifespan_context(app):
        db = app.state.db
        await repo.create_project(db, "solo", str(tmp_path))
        body = _form({"command": "/girder", "text": "run do a thing"})
        ts = str(int(time.time()))
        resp = await client.post(
            "/api/webhooks/slack",
            content=body,
            headers={
                "X-Slack-Request-Timestamp": ts,
                "X-Slack-Signature": sign(body, ts),
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        assert resp.status_code == 200
        run_id = resp.json()["text"].split("`")[1]
        from girder.db.models import RunStatus

        run = await _get_by_prefix(db, run_id)
        assert run is not None and run.status is RunStatus.SPEC_PENDING
        while app.state.background_tasks:
            import asyncio

            await asyncio.gather(*list(app.state.background_tasks))


async def _get_by_prefix(db: Database, prefix: str) -> object:
    rows = await db.fetchall("SELECT id FROM runs WHERE id LIKE ?", (prefix + "%",))
    if not rows:
        return None
    return await repo.get_run(db, rows[0]["id"])
