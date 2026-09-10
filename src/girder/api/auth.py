"""Static API-key auth for the console (WP 12.5) — minimal by design.

NOT OAuth, NOT sessions (explicit non-goals). When ``[web] api_key`` is
empty/unset the whole console stays anonymous (localhost default); when set,
every route on the aggregated router requires ``Authorization: Bearer <key>``
or ``X-API-Key: <key>`` (``?api_key=`` is also accepted on **every** route —
it exists because ``EventSource`` and plain HTML forms cannot set headers —
with the tradeoff that a key sent this way may land in access logs and browser
history for whatever route it was used on).

Enforcement is a single router-level dependency installed once in
:mod:`girder.api.routes` (not app middleware): the static asset ``/static``
mount lives outside the router and is therefore exempt structurally, and the
``/metrics`` + webhook exemptions below are one named constant. Key
comparison always goes through :func:`secrets.compare_digest`.
"""

from __future__ import annotations

import secrets as _secrets

from fastapi import HTTPException, Request

from girder.api.deps import SecretsDeps, SettingsDeps

# Path prefixes that bypass the API-key check entirely:
#   /metrics        — Prometheus scrape endpoint (WP 12.3); scrapers cannot be
#                     expected to hold the console key. (Also structurally
#                     exempt: the metrics router is mounted on the app, not
#                     the console router that carries the auth dependency.)
#   /api/webhooks/  — inbound webhooks (WS-05: /api/webhooks/github,
#                     /api/webhooks/slack) authenticate via HMAC signatures
#                     over the raw body, which is orthogonal to the
#                     caller-held console key. New exempt families go here.
PUBLIC_PATH_PREFIXES: tuple[str, ...] = ("/metrics", "/api/webhooks/")

_QUERY_PARAM = "api_key"  # fallback channel for plain HTML form POSTs


def is_public_path(path: str) -> bool:
    """True for paths exempt from the API-key check (see PUBLIC_PATH_PREFIXES).

    Entries ending in "/" are prefix matches; entries without a trailing "/"
    match only the exact path or anything under it — so ``/metricsEvil`` is
    NOT exempt despite sharing the ``/metrics`` prefix.
    """
    for prefix in PUBLIC_PATH_PREFIXES:
        if prefix.endswith("/"):
            if path.startswith(prefix):
                return True
        elif path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _presented_key(request: Request) -> str | None:
    """Extract the caller's key from Bearer / X-API-Key / ?api_key=."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header_key = request.headers.get("x-api-key")
    if header_key:
        return header_key.strip()
    return request.query_params.get(_QUERY_PARAM)


def require_api_key(request: Request, settings: SettingsDeps, secrets: SecretsDeps) -> None:
    """Router-level dependency: 401 missing credentials, 403 invalid key.

    The configured key is ``settings.web.api_key`` (girder.toml ``[web]
    api_key``, env ``GIRDER_WEB__API_KEY``, ``girder web --api-key``) falling
    back to ``secrets.web_api_key`` (``secrets.toml`` ``[web] api_key``).
    Empty/unset ⇒ auth disabled ⇒ anonymous access preserved.
    """
    expected = settings.web.api_key or secrets.web_api_key or ""
    if not expected or is_public_path(request.url.path):
        return
    presented = _presented_key(request)
    if presented is None:
        raise HTTPException(status_code=401, detail="missing API key")
    if not _secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=403, detail="invalid API key")
