"""SC-19 (WS-05): GitHub issue assignment → run → PR.

The full scenario ends on a real GitHub PR, which needs a live deployment
(public webhook route + repository token). This integration test exercises
everything that is exercisable offline, against a fixture git repo with a
GitHub remote URL:

1. a real fixture repo registered as a Girder project;
2. the DEFAULT project resolver (git remote → ``owner/name`` match) — no
   injection of the resolver;
3. a signed ``issues.assigned`` webhook through the real HTTP route;
4. the run created with the composed intent through the standard
   SPEC_PENDING pipeline, with spec generation drained;
5. the "Girder is working on this" comment recorded (in deployment this is
   the GitHubClient posting to the issue — faked here, the only step that
   genuinely requires live GitHub).

In deployment SC-19 completes with: GitHub webhook → run → agent delivery →
``open_pr`` on the real repo (covered by delivery-pump tests against a local
bare remote) and the run link comment posted back to the issue.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from girder.api.app import create_app
from girder.config import GithubConfig, Secrets, Settings
from girder.db import repo
from girder.db.engine import Database, default_migrations_dir
from girder.db.models import RunStatus

pytestmark = pytest.mark.integration

SECRET = "whsec_integration"


async def _make_fixture_repo(path: Path) -> None:
    """A real git repo whose origin is a (never-contacted) GitHub URL."""
    from girder.util import run_host_cmd

    path.mkdir(parents=True)  # noqa: ASYNC240 - fixture setup, not request handling
    for cmd in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
        ["git", "remote", "add", "origin", "https://github.com/acme/widget.git"],
    ):
        await run_host_cmd(["git", "-C", str(path), *cmd[1:]], timeout_s=30)


@pytest.fixture
async def app_with_repo(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, Database]]:
    repo_dir = tmp_path / "widget"
    await _make_fixture_repo(repo_dir)
    settings = Settings(github=GithubConfig(webhook_enabled=True, bot_account="girder-bot"))
    secrets = Secrets(github_webhook_secret=SECRET)
    app = create_app(
        db_path=tmp_path / "sc19.db",
        settings=settings,
        secrets=secrets,
        migrations_dir=default_migrations_dir(),
        generator_factory=lambda: _NoopGenerator(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client, app.router.lifespan_context(app):
        # register the fixture repo; the default webhook resolver matches it
        # by reading its git remote — no test injection
        resp = await client.post(
            "/api/projects", data={"name": "widget", "repo_path": str(repo_dir)}
        )
        assert resp.status_code == 303

        comments: list[tuple[int, str, str]] = []

        async def comment(number: int, full_name: str, body: str) -> None:
            comments.append((number, full_name, body))

        def factory() -> object:
            from girder.github.webhook import WebhookProcessor

            return WebhookProcessor(
                app.state.db,
                app.state.settings,
                app.state.secrets,
                comment=comment,  # only the GitHub POST is faked
            )

        app.state.webhook_processor_factory = factory
        yield client, app, comments  # type: ignore[misc]


class _NoopGenerator:
    async def generate(self, **kwargs: object) -> str:  # pragma: no cover
        return "---\nschema: girder.openspec/v1\ntitle: t\nintent: i\ntasks: []\n---\nx"


async def test_sc19_issue_assignment_creates_run(
    app_with_repo: tuple[httpx.AsyncClient, Any, list[tuple[int, str, str]]],
) -> None:
    client, app, comments = app_with_repo
    db: Database = app.state.db
    payload = {
        "action": "assigned",
        "assignee": {"login": "girder-bot"},
        "issue": {
            "number": 42,
            "title": "Add modulo endpoint",
            "body": "POST /modulo should return a % b.",
        },
        "repository": {"full_name": "acme/widget"},
    }
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    resp = await client.post(
        "/api/webhooks/github",
        content=body,
        headers={"X-Hub-Signature-256": signature, "X-GitHub-Event": "issues"},
    )
    assert resp.status_code == 200, resp.text
    outcome = resp.json()
    assert outcome["handled"] is True, outcome["reason"]
    run_id = outcome["run_id"]

    # drain background generation
    while app.state.background_tasks:
        await asyncio.gather(*list(app.state.background_tasks))

    run = await repo.get_run(db, run_id)
    assert run is not None
    assert run.status is RunStatus.SPEC_PENDING
    assert run.intent == "Add modulo endpoint\n\nPOST /modulo should return a % b."
    assert run.branch == f"run/{run.id[:8]}"
    # the working-on comment was posted back to the issue (faked transport)
    assert comments and comments[0][0] == 42
    assert f"/runs/{run_id}" in comments[0][2]
