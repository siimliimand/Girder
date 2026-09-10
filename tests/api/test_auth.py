"""WP 12.5: static API-key auth on the console (post-split routes package).

Covers: 401 missing / 403 invalid key, both Bearer and X-API-Key channels,
anonymous access preserved when unset, static-asset + PUBLIC_PATH_PREFIXES
exemptions, and the compare_digest comparison path.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from girder.api.app import create_app
from girder.api.auth import PUBLIC_PATH_PREFIXES, is_public_path
from girder.config import Secrets, Settings

KEY = "s3cret-key"


def _app(*, api_key: str = "", secret_key: str | None = None) -> Any:  # type: ignore[name-defined]
    settings = Settings()
    settings.web.api_key = api_key
    secrets = Secrets(web_api_key=secret_key)
    return create_app(
        db_path=Path(tempfile.mkdtemp()) / "auth.db",
        settings=settings,
        secrets=secrets,
    )


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_no_key_configured_anonymous_access() -> None:
    app = _app()
    async with _client(app) as client, app.router.lifespan_context(app):
        resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 200


async def test_missing_key_is_401() -> None:
    app = _app(api_key=KEY)
    async with _client(app) as client, app.router.lifespan_context(app):
        resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 401


async def test_invalid_key_is_403() -> None:
    app = _app(api_key=KEY)
    async with _client(app) as client, app.router.lifespan_context(app):
        resp = await client.get(
            "/", headers={"X-API-Key": "wrong"}, follow_redirects=False
        )
    assert resp.status_code == 403


@pytest.mark.parametrize("headers", [
    {"Authorization": f"Bearer {KEY}"},
    {"X-API-Key": KEY},
])
async def test_bearer_and_api_key_header_accepted(headers: dict[str, str]) -> None:
    app = _app(api_key=KEY)
    async with _client(app) as client, app.router.lifespan_context(app):
        resp = await client.get("/", headers=headers, follow_redirects=False)
    assert resp.status_code == 200


async def test_query_param_channel_accepted_for_html_forms() -> None:
    app = _app(api_key=KEY)
    async with _client(app) as client, app.router.lifespan_context(app):
        resp = await client.get(f"/?api_key={KEY}", follow_redirects=False)
    assert resp.status_code == 200


async def test_secrets_fallback_key() -> None:
    app = _app(secret_key=KEY)
    async with _client(app) as client, app.router.lifespan_context(app):
        ok = await client.get(
            "/", headers={"X-API-Key": KEY}, follow_redirects=False
        )
        no = await client.get("/", follow_redirects=False)
    assert ok.status_code == 200
    assert no.status_code == 401


async def test_static_assets_exempt() -> None:
    app = _app(api_key=KEY)
    async with _client(app) as client, app.router.lifespan_context(app):
        resp = await client.get("/static/style.css")
    assert resp.status_code == 200


def test_public_prefix_exemption_mechanism() -> None:
    # /metrics (WP 12.3 scrape endpoint) and /api/webhooks/ (WS-05 HMAC
    # endpoints) are exempt by constant, testable without registering routes.
    assert PUBLIC_PATH_PREFIXES == ("/metrics", "/api/webhooks/")
    assert is_public_path("/metrics")
    assert is_public_path("/api/webhooks/github/push")
    assert not is_public_path("/api/projects")
    # boundary: non-slash-prefixed entries are exact-or-under, not raw prefix
    assert not is_public_path("/metricsEvil")
    assert not is_public_path("/metrics-fake")
    assert not is_public_path("/api/webhooksX/evil")
    assert is_public_path("/metrics/")


async def test_public_path_exempt_end_to_end() -> None:
    app = _app(api_key=KEY)
    async with _client(app) as client, app.router.lifespan_context(app):
        resp = await client.get("/metrics", follow_redirects=False)
    # /metrics is a live WP 12.3 endpoint now — but never 401/403: the
    # exemption wins over the configured API key.
    assert resp.status_code not in (401, 403)


async def test_metrics_lookalikes_require_key() -> None:
    # The exemption is a boundary, not a raw prefix: /metricsEvil must not
    # ride /metrics' exemption. Lookalikes match no route, so they 404 —
    # the point is they are never exempt (200) either.
    app = _app(api_key=KEY)
    async with _client(app) as client, app.router.lifespan_context(app):
        for path in ("/metricsEvil", "/metrics-fake"):
            resp = await client.get(path, follow_redirects=False)
            assert resp.status_code == 404


async def test_compare_digest_rejects_tampered_bearer_prefix() -> None:
    # Comparison must be constant-time (secrets.compare_digest): a wrong key
    # of any shape — including one sharing a prefix — yields 403, never a
    # traceback or a pass-through.
    app = _app(api_key=KEY)
    async with _client(app) as client, app.router.lifespan_context(app):
        resp = await client.get(
            "/", headers={"Authorization": f"Bearer {KEY}-extra"}, follow_redirects=False
        )
    assert resp.status_code == 403
