"""WP 10.1 regression guards: the routes.py split must be behavior-neutral.

Asserts the registered route set (path + methods) is identical to the
pre-split monolithic ``routes.py`` and that the legacy import surface
(``girder.api.routes.X``) still resolves.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.routing import APIRoute

from girder.api.app import create_app

# Frozen snapshot of the pre-split router (derived from the monolithic
# girder/api/routes.py before WP 10.1). (methods, path) with methods sorted.
PRE_SPLIT_ROUTES: set[tuple[tuple[str, ...], str]] = {
    (("GET",), "/"),
    (("GET",), "/amendments"),
    (("GET",), "/api/history"),
    (("GET",), "/api/merge-queue"),
    (("GET",), "/api/projects"),
    (("GET",), "/api/projects/{pid}"),
    (("GET",), "/api/runs/{rid}"),
    (("GET",), "/api/runs/{rid}/amendments"),
    (("GET",), "/api/runs/{rid}/diffs"),
    (("GET",), "/api/runs/{rid}/events"),
    (("GET",), "/api/runs/{rid}/graph"),
    (("GET",), "/api/runs/{rid}/spend"),
    (("GET",), "/history"),
    (("GET",), "/merge-queue"),
    (("GET",), "/projects/{pid}"),
    (("GET",), "/projects/{pid}/tier"),
    (("GET",), "/runs/{rid}"),
    (("GET",), "/runs/{rid}/panel"),
    (("GET",), "/runs/{rid}/postmortem"),
    (("POST",), "/api/projects"),
    (("POST",), "/api/projects/{pid}/runs"),
    (("POST",), "/api/projects/{pid}/tier"),
    (("POST",), "/api/runs/{rid}/amendments/{aid}/abort"),
    (("POST",), "/api/runs/{rid}/amendments/{aid}/approve"),
    (("POST",), "/api/runs/{rid}/amendments/{aid}/reject"),
    (("POST",), "/api/runs/{rid}/approve"),
    (("POST",), "/api/runs/{rid}/edit"),
    (("POST",), "/api/runs/{rid}/merge"),
    (("POST",), "/api/runs/{rid}/regenerate"),
    (("POST",), "/api/runs/{rid}/reviewed"),
    (("POST",), "/api/runs/{rid}/steer"),
}


def _walk_routes(routes: list[Any], out: set[tuple[tuple[str, ...], str]]) -> None:
    for r in routes:
        methods = getattr(r, "methods", None)
        path = getattr(r, "path", None)
        if methods and path:
            out.add((tuple(sorted(methods)), path))
            continue
        # fastapi >=0.13x wraps include_router in _IncludedRouter; Mount has .routes/.app
        inner = getattr(r, "original_router", None) or getattr(r, "routes", None)
        if inner is not None:
            _walk_routes(list(inner.routes) if hasattr(inner, "routes") else list(inner), out)
        elif getattr(r, "app", None) is not None and hasattr(r.app, "routes"):
            _walk_routes(list(r.app.routes), out)


@pytest.fixture(scope="module")
def app_routes() -> set[tuple[tuple[str, ...], str]]:
    app = create_app(db_path=Path(tempfile.mkdtemp()) / "wp101.db")
    routes: set[tuple[tuple[str, ...], str]] = set()
    _walk_routes(list(app.routes), routes)
    return routes


def test_registered_route_set_unchanged(app_routes: set[tuple[tuple[str, ...], str]]) -> None:
    console = {
        (m, p)
        for (m, p) in app_routes
        if not p.startswith(("/docs", "/openapi", "/redoc"))
    }
    assert console == PRE_SPLIT_ROUTES


def test_every_console_route_is_an_api_route() -> None:
    app = create_app(db_path=Path(tempfile.mkdtemp()) / "wp101b.db")
    api_routes: list[APIRoute] = []

    def collect(routes: list[Any]) -> None:
        for r in routes:
            if isinstance(r, APIRoute):
                api_routes.append(r)
                continue
            inner = getattr(r, "original_router", None)
            if inner is not None:
                collect(list(inner.routes))
            elif hasattr(r, "routes"):
                collect(list(r.routes))
            elif getattr(r, "app", None) is not None and hasattr(r.app, "routes"):
                collect(list(r.app.routes))

    collect(list(app.routes))
    assert {(tuple(sorted(x.methods)), x.path) for x in api_routes} == PRE_SPLIT_ROUTES


def test_legacy_import_surface() -> None:
    from girder.api import routes as legacy
    from girder.api.routes import _next_generation_estimate as _  # noqa: F401

    assert hasattr(legacy, "router")
    # Names that lived at module level pre-split keep their exact spelling.
    for name in (
        "_require_run",
        "_require_project",
        "_run_context",
        "_panel_context",
        "_estimate_ctx",
        "_proposal_task_rows",
        "_next_generation_estimate",
        "_next_generation_prompt_chars",
        "_tier1_role",
        "_merge_queue",
        "_merged_rows",
        "_amendments_inbox",
        "_history",
        "_excerpt",
        "_github_for",
        "_project_json",
        "_project_timestamps",
        "_graph",
        "_task_view",
        "_event_class",
        "_redact",
        "_redact_value",
        "_review_window_state",
        "_review_window_message",
    ):
        assert hasattr(legacy, name), name


def test_no_route_module_exceeds_600_lines() -> None:
    import girder.api as api_pkg

    api_dir = Path(api_pkg.__file__).parent
    files = [api_dir / "deps.py", *(api_dir / "routes").glob("*.py")]
    assert len(files) >= 9
    for f in files:
        assert len(f.read_text().splitlines()) <= 600, str(f)
