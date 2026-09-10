"""Unit tests for failure-artifact storage (raw model output on
spec_generation_failed) and its display on the run page.

No network, no git: the failing spec generator is injected via
``create_app(generator_factory=...)`` and the background generation task is
awaited directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from girder.api.app import _generate, create_app
from girder.config import Secrets, Settings
from girder.db import repo
from girder.db.engine import Database, default_migrations_dir
from girder.specs.generator import SpecGenerationError

TRUNCATION_NOTE = "\n[...truncated by girder at 20000 characters]"


class _FailingGenerator:
    """SpecGenerator stand-in whose generate() always fails."""

    def __init__(self, raw_output: str | None) -> None:
        self._raw_output = raw_output

    async def generate(self, **kwargs: Any) -> str:
        raise SpecGenerationError(
            ["document must start with a '---' frontmatter delimiter line"],
            raw_output=self._raw_output,
        )


def make_app(tmp_path: Path, raw_output: str | None) -> Any:
    return create_app(
        db_path=tmp_path / "artifact.db",
        settings=Settings(),
        secrets=Secrets(),
        migrations_dir=default_migrations_dir(),
        generator_factory=lambda: _FailingGenerator(raw_output),
    )


def make_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=False
    )


async def _seed_and_fail(app: Any, raw_output: str | None) -> Any:
    """Create a project+run, run the failing generation, return the run."""
    db: Database = app.state.db
    p = await repo.create_project(db, "proj", ".")
    r = await repo.create_run(db, p.id, "Ship it", branch="run/abc", budget_cap_usd=5.0)
    await _generate(app, r.id, None)
    return r


async def test_failed_generation_records_raw_output_with_truncation(tmp_path: Path) -> None:
    app = make_app(tmp_path, "x" * 25_000)
    async with make_client(app), app.router.lifespan_context(app):
        r = await _seed_and_fail(app, "x" * 25_000)
        db: Database = app.state.db
        rows = await db.fetchall(
            "SELECT payload_json FROM agent_events "
            "WHERE run_id = ? AND event_type = 'spec_generation_failed'",
            (r.id,),
        )
        assert len(rows) == 1
        payload = json.loads(rows[0]["payload_json"])
        assert "frontmatter delimiter" in payload["error"]
        assert payload["raw_output"] == "x" * 20_000 + TRUNCATION_NOTE


async def test_failed_generation_records_short_raw_output_verbatim(tmp_path: Path) -> None:
    app = make_app(tmp_path, "---\nproposed: maybe\n---\nbroken body")
    async with make_client(app), app.router.lifespan_context(app):
        r = await _seed_and_fail(app, "---\nproposed: maybe\n---\nbroken body")
        db: Database = app.state.db
        rows = await db.fetchall(
            "SELECT payload_json FROM agent_events "
            "WHERE run_id = ? AND event_type = 'spec_generation_failed'",
            (r.id,),
        )
        payload = json.loads(rows[0]["payload_json"])
        assert payload["raw_output"] == "---\nproposed: maybe\n---\nbroken body"
        assert TRUNCATION_NOTE not in payload["raw_output"]


async def test_run_page_renders_failed_output_block(tmp_path: Path) -> None:
    app = make_app(tmp_path, "x" * 25_000)
    async with make_client(app) as client, app.router.lifespan_context(app):
        r = await _seed_and_fail(app, "x" * 25_000)
        resp = await client.get(f"/runs/{r.id}")
        assert resp.status_code == 200
        assert "Last failed model output" in resp.text
        assert "truncated at 20000 characters" in resp.text
        panel = await client.get(f"/runs/{r.id}/panel")
        assert panel.status_code == 200
        assert "Last failed model output" in panel.text


async def test_run_page_omits_block_without_raw_output(tmp_path: Path) -> None:
    app = make_app(tmp_path, None)
    async with make_client(app) as client, app.router.lifespan_context(app):
        r = await _seed_and_fail(app, None)
        db: Database = app.state.db
        rows = await db.fetchall(
            "SELECT payload_json FROM agent_events "
            "WHERE run_id = ? AND event_type = 'spec_generation_failed'",
            (r.id,),
        )
        assert len(rows) == 1
        assert "raw_output" not in json.loads(rows[0]["payload_json"])
        resp = await client.get(f"/runs/{r.id}")
        assert resp.status_code == 200
        assert "Generation failed" in resp.text
        assert "Last failed model output" not in resp.text


async def test_successful_generation_hides_stale_failure_banner(tmp_path: Path) -> None:
    """Regression (real incident): after a failed generate followed by a
    successful regenerate, the panel kept showing 'Generation failed' from the
    superseded attempt. Only the LATEST generation outcome may claim the
    panel — a newer success hides the failure banner and its artifact."""
    app = make_app(tmp_path, "rejected draft")  # factory unused; events seeded by hand
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await repo.create_project(db, "proj", ".")
        r = await repo.create_run(db, p.id, "Ship it", branch="run/abc", budget_cap_usd=5.0)

        await repo.insert_event(
            db, "spec_generation_failed", {"error": "model produced no content"}, run_id=r.id
        )
        page = await client.get(f"/runs/{r.id}")
        assert "Generation failed" in page.text  # latest outcome is the failure

        await repo.insert_event(db, "spec_generation_finished", {"chars": 5975}, run_id=r.id)
        page = await client.get(f"/runs/{r.id}")
        assert "Generation failed" not in page.text
        panel = await client.get(f"/runs/{r.id}/panel")
        assert "Generation failed" not in panel.text
        assert "Last failed model output" not in panel.text


async def test_later_failure_supersedes_earlier_success(tmp_path: Path) -> None:
    """The suppression is symmetric: a failure AFTER a success is the latest
    outcome and must still show."""
    app = make_app(tmp_path, None)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await repo.create_project(db, "proj", ".")
        r = await repo.create_run(db, p.id, "Ship it", branch="run/abc", budget_cap_usd=5.0)
        await repo.insert_event(db, "spec_generation_finished", {"chars": 100}, run_id=r.id)
        await repo.insert_event(
            db, "spec_generation_failed", {"error": "model produced no content"}, run_id=r.id
        )
        resp = await client.get(f"/runs/{r.id}")
        assert "Generation failed" in resp.text
