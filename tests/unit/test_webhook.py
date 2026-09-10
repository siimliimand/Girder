"""Unit tests for the GitHub webhook receiver (WS-05 / WP 9.1).

Signature verification runs before anything else (403, nothing persisted);
per-event handlers are exercised through a recording fake over the REAL
run-creation pipeline, plus one route-level test through the full app.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from girder.api.app import create_app
from girder.config import GithubConfig, Secrets, Settings
from girder.db import repo
from girder.db.engine import Database, default_migrations_dir
from girder.db.models import RunStatus
from girder.github.webhook import WebhookProcessor, verify_github_signature

SECRET = "whsec_unit_test"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ HMAC


def test_signature_valid() -> None:
    body = b'{"action": "assigned"}'
    assert verify_github_signature(SECRET, body, sign(body))


def test_signature_tampered_rejected() -> None:
    body = b'{"action": "assigned"}'
    assert not verify_github_signature(SECRET, body + b" ", sign(body))
    assert not verify_github_signature(SECRET, body, sign(body, "wrong-secret"))


def test_signature_missing_secret_or_header_rejected() -> None:
    body = b"{}"
    assert not verify_github_signature(None, body, sign(body))
    assert not verify_github_signature(SECRET, body, None)
    assert not verify_github_signature(SECRET, body, "md5=deadbeef")
    assert not verify_github_signature("", body, sign(body))


# ------------------------------------------------------- processor handlers


def _issue_payload(
    *,
    action: str = "assigned",
    assignee: str = "girder-bot",
    title: str = "Add division",
    body_text: str = "Users must be able to divide numbers.",
    full_name: str = "acme/widget",
    number: int = 7,
) -> dict[str, Any]:
    return {
        "action": action,
        "assignee": {"login": assignee},
        "issue": {"number": number, "title": title, "body": body_text},
        "repository": {"full_name": full_name},
    }


@dataclass
class Fakes:
    """Records runs/comments; run creation goes through the REAL db pipeline
    (a registered project) so agent_events foreign keys hold."""

    db: Database
    runs: list[Any] = field(default_factory=list)
    comments: list[tuple[int, str, str]] = field(default_factory=list)

    async def resolve(self, full_name: str) -> Any:
        if full_name != "acme/widget":
            return None
        return await repo.get_project_by_name(self.db, "acme")

    async def create_run(self, project: Any, intent: str) -> Any:
        from girder.github.webhook import _default_create_run

        settings = Settings(github=GithubConfig(webhook_enabled=True, bot_account="girder-bot"))
        run = await _default_create_run(self.db, settings, project, intent)
        self.runs.append(run)
        return run

    async def comment(self, number: int, full_name: str, body: str) -> None:
        self.comments.append((number, full_name, body))


def make_processor(db: Database, fakes: Fakes, **config: Any) -> WebhookProcessor:
    settings = Settings(
        github=GithubConfig(webhook_enabled=True, bot_account="girder-bot", **config)
    )
    return WebhookProcessor(
        db,
        settings,
        Secrets(),
        resolve_project=fakes.resolve,  # type: ignore[arg-type]
        create_run=fakes.create_run,  # type: ignore[arg-type]
        comment=fakes.comment,  # type: ignore[arg-type]
    )


async def _fixture_project(db: Database) -> None:
    await repo.create_project(db, "acme", "/tmp/nowhere")


async def test_issues_assigned_to_bot_creates_run(db: Database) -> None:
    await _fixture_project(db)
    fakes = Fakes(db)
    outcome = await make_processor(db, fakes).handle("issues", _issue_payload())
    assert outcome.handled
    assert fakes.runs[0].intent == "Add division\n\nUsers must be able to divide numbers."
    # working-on comment posted on the issue, linking the real console URL
    assert fakes.comments[0][0] == 7
    assert "view run" in fakes.comments[0][2]
    assert f"runs/{fakes.runs[0].id}" in fakes.comments[0][2]
    # the run landed in the db in SPEC_PENDING (console pipeline parity)
    fresh = await repo.get_run(db, fakes.runs[0].id)
    assert fresh is not None and fresh.status is RunStatus.SPEC_PENDING
    event = await repo.get_latest_event(db, fakes.runs[0].id, "github_webhook_run_created")
    assert event is not None


async def test_non_bot_assignment_ignored(db: Database) -> None:
    await _fixture_project(db)
    fakes = Fakes(db)
    outcome = await make_processor(db, fakes).handle(
        "issues", _issue_payload(assignee="someone-else")
    )
    assert not outcome.handled
    assert outcome.run_id is None
    assert not fakes.runs and not fakes.comments


async def test_unassigned_action_ignored(db: Database) -> None:
    await _fixture_project(db)
    fakes = Fakes(db)
    outcome = await make_processor(db, fakes).handle("issues", _issue_payload(action="closed"))
    assert not outcome.handled


async def test_girder_comment_creates_run_with_inline_intent(db: Database) -> None:
    await _fixture_project(db)
    fakes = Fakes(db)
    payload = _issue_payload(title="whatever", body_text="")
    payload.pop("assignee")
    payload["comment"] = {"body": "/girder add a modulo endpoint", "user": {"login": "alice"}}
    outcome = await make_processor(db, fakes).handle("issue_comment", payload)
    assert outcome.handled
    assert fakes.runs[0].intent == "add a modulo endpoint"


async def test_non_matching_comment_ignored(db: Database) -> None:
    await _fixture_project(db)
    fakes = Fakes(db)
    payload = _issue_payload()
    payload.pop("assignee")
    payload["comment"] = {"body": "just chatting", "user": {"login": "alice"}}
    outcome = await make_processor(db, fakes).handle("issue_comment", payload)
    assert not outcome.handled
    assert not fakes.runs


async def test_bot_own_comment_ignored_no_loop(db: Database) -> None:
    await _fixture_project(db)
    fakes = Fakes(db)
    payload = _issue_payload()
    payload.pop("assignee")
    payload["comment"] = {"body": "/girder echo", "user": {"login": "girder-bot"}}
    outcome = await make_processor(db, fakes).handle("issue_comment", payload)
    assert not outcome.handled  # loop prevention


async def test_review_comment_fix_this_creates_scoped_run(db: Database) -> None:
    await _fixture_project(db)
    fakes = Fakes(db)
    payload = {
        "action": "created",
        "comment": {
            "body": "/girder fix this",
            "path": "src/calc/divide.py",
            "user": {"login": "alice"},
        },
        "pull_request": {"number": 12, "title": "Division support"},
        "repository": {"full_name": "acme/widget"},
    }
    outcome = await make_processor(db, fakes).handle("pull_request_review_comment", payload)
    assert outcome.handled
    assert "PR #12" in fakes.runs[0].intent
    assert "src/calc/divide.py" in fakes.runs[0].intent
    assert fakes.comments[0][0] == 12  # commented on the PR


async def test_other_review_comment_ignored(db: Database) -> None:
    await _fixture_project(db)
    fakes = Fakes(db)
    payload = {
        "action": "created",
        "comment": {"body": "nit: rename", "path": "x.py", "user": {"login": "alice"}},
        "pull_request": {"number": 12, "title": "T"},
        "repository": {"full_name": "acme/widget"},
    }
    outcome = await make_processor(db, fakes).handle("pull_request_review_comment", payload)
    assert not outcome.handled


async def test_unknown_event_ignored(db: Database) -> None:
    fakes = Fakes(db)
    outcome = await make_processor(db, fakes).handle("push", {"ref": "refs/heads/main"})
    assert not outcome.handled  # route still answers 200
    assert not fakes.runs


async def test_unknown_repository_ignored(db: Database) -> None:
    await _fixture_project(db)
    fakes = Fakes(db)
    outcome = await make_processor(db, fakes).handle(
        "issues", _issue_payload(full_name="acme/other")
    )
    assert not outcome.handled


# --------------------------------------------------------------------- route


class _NoopGenerator:
    async def generate(self, **kwargs: Any) -> str:  # pragma: no cover
        return "---\nschema: girder.openspec/v1\ntitle: t\nintent: i\ntasks: []\n---\nx"


async def test_route_signature_gate_and_run_creation(tmp_path: Path, db: Database) -> None:
    """Full-app test: tampered signature 403s with nothing persisted; a valid
    issues.assigned delivery creates a run and dispatches generation."""
    settings = Settings(github=GithubConfig(webhook_enabled=True, bot_account="girder-bot"))
    secrets = Secrets(github_webhook_secret=SECRET)
    fakes = Fakes(db)
    await repo.create_project(db, "acme", str(tmp_path))
    app = create_app(
        db_path=tmp_path / "api.db",
        settings=settings,
        secrets=secrets,
        migrations_dir=default_migrations_dir(),
        generator_factory=lambda: _NoopGenerator(),
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client, app.router.lifespan_context(app):
        app.state.webhook_processor_factory = lambda: make_processor(db, fakes)  # type: ignore[arg-type,return-value]
        body = json.dumps(_issue_payload()).encode()

        tampered = await client.post(
            "/api/webhooks/github",
            content=body,
            headers={
                "X-Hub-Signature-256": sign(body, "wrong-secret"),
                "X-GitHub-Event": "issues",
            },
        )
        assert tampered.status_code == 403
        row = await db.fetchone("SELECT COUNT(*) AS c FROM runs")
        assert row is not None and row["c"] == 0  # nothing persisted

        unknown = await client.post(
            "/api/webhooks/github",
            content=b'{"zen": "hi"}',
            headers={
                "X-Hub-Signature-256": sign(b'{"zen": "hi"}'),
                "X-GitHub-Event": "ping",
            },
        )
        assert unknown.status_code == 200
        assert unknown.json()["handled"] is False

        ok = await client.post(
            "/api/webhooks/github",
            content=body,
            headers={"X-Hub-Signature-256": sign(body), "X-GitHub-Event": "issues"},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["handled"] is True
        while app.state.background_tasks:
            await asyncio.gather(*list(app.state.background_tasks))

    assert len(fakes.runs) == 1
    assert fakes.runs[0].intent.startswith("Add division")


# ------------------------------------------------- default pipeline (real db)


async def test_default_create_run_pipeline(db: Database) -> None:
    """The default run factory walks the same pipeline as the web console."""
    from girder.github.webhook import _default_create_run

    project = await repo.create_project(db, "acme", "/tmp/nowhere")
    settings = Settings(github=GithubConfig(webhook_enabled=True, bot_account="b"))
    run = await _default_create_run(db, settings, project, "do the thing")
    fresh = await repo.get_run(db, run.id)
    assert fresh is not None
    assert fresh.status is RunStatus.SPEC_PENDING
    assert fresh.branch == f"run/{run.id[:8]}"
