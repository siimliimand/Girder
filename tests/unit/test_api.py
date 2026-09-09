"""Unit tests for the approval web console (impl-plan §10).

No git, no network: generation is a fake injected via generator_factory, so
approve-and-freeze is covered in tests/integration/test_api_approval.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from girder.api.app import create_app
from girder.config import Secrets, Settings
from girder.db import repo
from girder.db.engine import Database, default_migrations_dir
from girder.specs.validator import SpecValidationError, parse_spec

GOLDEN = """\
---
schema: girder.openspec/v1
title: Add user authentication
intent: Users must be able to log in with a token.
tasks:
  - id: auth-endpoint
    title: Implement token endpoint
    type: code_change
    scope_globs: ["src/auth/**"]
    success_criteria:
      - "POST /token returns a JWT for valid credentials"
      - "expired tokens are rejected with 401"
    depends_on: []
---
Narrative.
"""


@dataclass
class _FakeGenerator:
    """Structural SpecGeneratorLike: records calls, returns a valid doc."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    fail: bool = False

    async def generate(
        self,
        *,
        run: Any,
        project: Any,
        repo_path: Path,
        feedback: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "run_id": run.id,
                "intent": run.intent,
                "repo_path": repo_path,
                "feedback": feedback,
            }
        )
        if self.fail:
            raise SpecValidationError(["boom"])
        return GOLDEN


def make_app(tmp_path: Path, fake: _FakeGenerator) -> Any:
    return create_app(
        db_path=tmp_path / "api.db",
        settings=Settings(),
        secrets=Secrets(),
        migrations_dir=default_migrations_dir(),
        generator_factory=lambda: fake,
    )


def make_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=False
    )


async def drain(app: Any) -> None:
    """Wait for background generation tasks to finish."""
    while app.state.background_tasks:
        await asyncio.gather(*list(app.state.background_tasks))
        await asyncio.sleep(0)


async def make_project(client: httpx.AsyncClient, tmp_path: Path, name: str = "proj") -> str:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(exist_ok=True)
    resp = await client.post(
        "/api/projects", data={"name": name, "repo_path": str(repo_dir)}
    )
    assert resp.status_code == 303, resp.text
    page = await client.get("/")
    for line in page.text.split("\n"):
        if "/projects/" in line and name in line:
            return line.split('href="/projects/')[1].split('"')[0]
    raise AssertionError("project link not found on index")


async def make_run(client: httpx.AsyncClient, pid: str) -> str:
    resp = await client.post(f"/api/projects/{pid}/runs", data={"intent": "Ship it"})
    assert resp.status_code == 303
    return resp.headers["location"].rsplit("/", 1)[-1]


async def test_create_project_and_index(tmp_path: Path) -> None:
    fake = _FakeGenerator()
    app = make_app(tmp_path, fake)
    async with make_client(app) as client, app.router.lifespan_context(app):
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        resp = await client.post("/api/projects", data={"name": "acme", "repo_path": str(repo_dir)})
        assert resp.status_code == 303
        page = await client.get("/")
        assert page.status_code == 200
        assert "acme" in page.text


async def test_create_project_bad_repo_path(tmp_path: Path) -> None:
    app = make_app(tmp_path, _FakeGenerator())
    async with make_client(app) as client, app.router.lifespan_context(app):
        resp = await client.post(
            "/api/projects", data={"name": "x", "repo_path": str(tmp_path / "nope")}
        )
        assert resp.status_code == 400
        assert "not an existing directory" in resp.text


async def test_create_project_duplicate_name(tmp_path: Path) -> None:
    app = make_app(tmp_path, _FakeGenerator())
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    async with make_client(app) as client, app.router.lifespan_context(app):
        first = await client.post(
            "/api/projects", data={"name": "dup", "repo_path": str(repo_dir)}
        )
        assert first.status_code == 303
        dup = await client.post("/api/projects", data={"name": "dup", "repo_path": str(repo_dir)})
        assert dup.status_code == 400
        assert "already exists" in dup.text


async def test_run_lifecycle_generate(tmp_path: Path) -> None:
    fake = _FakeGenerator()
    app = make_app(tmp_path, fake)
    async with make_client(app) as client, app.router.lifespan_context(app):
        pid = await make_project(client, tmp_path)
        rid = await make_run(client, pid)
        db: Database = app.state.db
        run = await repo.get_run(db, rid)
        assert run is not None
        assert run.status.value == "spec_pending"
        assert run.branch == f"run/{run.id[:8]}"

        await drain(app)
        assert len(fake.calls) == 1
        assert fake.calls[0]["feedback"] is None

        meta = await client.get(f"/api/runs/{rid}")
        assert meta.status_code == 200
        body = meta.json()
        assert body["has_proposal"] is True
        assert body["status"] == "spec_pending"

        page = await client.get(f"/runs/{rid}")
        assert page.status_code == 200
        assert "Add user authentication" in page.text

        panel = await client.get(f"/runs/{rid}/panel")
        assert panel.status_code == 200
        assert 'data-poll="false"' in panel.text


async def test_regenerate_with_feedback(tmp_path: Path) -> None:
    fake = _FakeGenerator()
    app = make_app(tmp_path, fake)
    async with make_client(app) as client, app.router.lifespan_context(app):
        pid = await make_project(client, tmp_path)
        rid = await make_run(client, pid)
        await drain(app)
        assert len(fake.calls) == 1

        resp = await client.post(f"/api/runs/{rid}/regenerate", data={"feedback": "make it short"})
        assert resp.status_code == 303

        await drain(app)
        assert len(fake.calls) == 2
        assert fake.calls[1]["feedback"] == "make it short"
        meta = await client.get(f"/api/runs/{rid}")
        assert meta.json()["has_proposal"] is True


async def test_regenerate_requires_spec_pending(tmp_path: Path) -> None:
    fake = _FakeGenerator()
    app = make_app(tmp_path, fake)
    async with make_client(app) as client, app.router.lifespan_context(app):
        pid = await make_project(client, tmp_path)
        rid = await make_run(client, pid)
        await drain(app)
        # move the run out of spec_pending via raw SQL (fsm bypass, test-only)
        db: Database = app.state.db
        await db.execute("UPDATE runs SET status = 'failed' WHERE id = ?", (rid,))
        await db.conn.commit()
        resp = await client.post(f"/api/runs/{rid}/regenerate", data={"feedback": "nope"})
        assert resp.status_code == 400
        assert "cannot regenerate" in resp.text
        assert len(fake.calls) == 1


async def test_edit_invalid_doc_rejected(tmp_path: Path) -> None:
    fake = _FakeGenerator()
    app = make_app(tmp_path, fake)
    async with make_client(app) as client, app.router.lifespan_context(app):
        pid = await make_project(client, tmp_path)
        rid = await make_run(client, pid)
        await drain(app)
        original = await client.get(f"/api/runs/{rid}")
        assert original.json()["has_proposal"] is True

        resp = await client.post(f"/api/runs/{rid}/edit", data={"proposal": "not a spec"})
        assert resp.status_code == 400
        assert 'class="error"' in resp.text
        # proposal unchanged
        meta = await client.get(f"/api/runs/{rid}")
        assert meta.json()["has_proposal"] is True


async def test_edit_valid_doc_updates_proposal(tmp_path: Path) -> None:
    fake = _FakeGenerator()
    app = make_app(tmp_path, fake)
    async with make_client(app) as client, app.router.lifespan_context(app):
        pid = await make_project(client, tmp_path)
        rid = await make_run(client, pid)
        await drain(app)
        modified = GOLDEN.replace("Add user authentication", "Renamed title")
        parse_spec(modified)  # sanity
        resp = await client.post(f"/api/runs/{rid}/edit", data={"proposal": modified})
        assert resp.status_code == 303
        page = await client.get(f"/runs/{rid}")
        assert "Renamed title" in page.text


async def test_edit_frozen_run_refused(tmp_path: Path) -> None:
    fake = _FakeGenerator()
    app = make_app(tmp_path, fake)
    async with make_client(app) as client, app.router.lifespan_context(app):
        pid = await make_project(client, tmp_path)
        rid = await make_run(client, pid)
        await drain(app)
        # freeze the run via raw SQL (test-only fsm bypass)
        db: Database = app.state.db
        await db.execute(
            "UPDATE runs SET spec_hash = 'deadbeef' WHERE id = ?", (rid,)
        )
        await db.conn.commit()
        before = await repo.get_run(db, rid)
        assert before is not None
        original_proposal = before.proposal_md

        resp = await client.post(f"/api/runs/{rid}/edit", data={"proposal": GOLDEN})
        assert resp.status_code == 409
        assert "frozen" in resp.text
        after = await repo.get_run(db, rid)
        assert after is not None
        assert after.proposal_md == original_proposal


async def test_edit_unfrozen_run_still_works(tmp_path: Path) -> None:
    fake = _FakeGenerator()
    app = make_app(tmp_path, fake)
    async with make_client(app) as client, app.router.lifespan_context(app):
        pid = await make_project(client, tmp_path)
        rid = await make_run(client, pid)
        await drain(app)
        db: Database = app.state.db
        run = await repo.get_run(db, rid)
        assert run is not None
        assert run.spec_hash is None  # unfrozen

        modified = GOLDEN.replace("Add user authentication", "Edited while open")
        resp = await client.post(f"/api/runs/{rid}/edit", data={"proposal": modified})
        assert resp.status_code == 303
        after = await repo.get_run(db, rid)
        assert after is not None
        assert after.proposal_md == modified


async def test_spend_endpoint(tmp_path: Path) -> None:
    fake = _FakeGenerator()
    app = make_app(tmp_path, fake)
    async with make_client(app) as client, app.router.lifespan_context(app):
        pid = await make_project(client, tmp_path)
        rid = await make_run(client, pid)
        resp = await client.get(f"/api/runs/{rid}/spend")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cap"] == 5.0
        assert body["spend_usd"] == 0.0
        assert body["projected_spend_usd"] == 0.0
        assert body["usage"] == []


async def test_unknown_run_404(tmp_path: Path) -> None:
    app = make_app(tmp_path, _FakeGenerator())
    async with make_client(app) as client, app.router.lifespan_context(app):
        assert (await client.get("/runs/missing")).status_code == 404
        assert (await client.get("/api/runs/missing")).status_code == 404
        assert (await client.get("/runs/missing/panel")).status_code == 404


async def test_unknown_project_run_creation_404(tmp_path: Path) -> None:
    app = make_app(tmp_path, _FakeGenerator())
    async with make_client(app) as client, app.router.lifespan_context(app):
        resp = await client.post("/api/projects/nope/runs", data={"intent": "x"})
        assert resp.status_code == 404
        assert (await client.get("/projects/nope")).status_code == 404


async def test_generation_failure_recorded(tmp_path: Path) -> None:
    fake = _FakeGenerator(fail=True)
    app = make_app(tmp_path, fake)
    async with make_client(app) as client, app.router.lifespan_context(app):
        pid = await make_project(client, tmp_path)
        rid = await make_run(client, pid)
        await drain(app)
        meta = await client.get(f"/api/runs/{rid}")
        assert meta.json()["has_proposal"] is False
        panel = await client.get(f"/runs/{rid}/panel")
        assert panel.status_code == 200
        assert "Generation failed" in panel.text


async def test_project_json_endpoints(tmp_path: Path) -> None:
    """§10: GET /api/projects[/{id}] return project JSON (list includes a
    created project; single-get returns it; unknown id ⇒ 404)."""
    fake = _FakeGenerator()
    app = make_app(tmp_path, fake)
    async with make_client(app) as client, app.router.lifespan_context(app):
        pid = await make_project(client, tmp_path, name="jsonproj")

        listed = await client.get("/api/projects")
        assert listed.status_code == 200
        items = listed.json()
        assert isinstance(items, list)
        matches = [p for p in items if p["id"] == pid]
        assert len(matches) == 1
        row = matches[0]
        assert row["name"] == "jsonproj"
        assert row["autonomy_tier"] == 0
        assert row["clean_merge_streak"] == 0
        assert row["repo_path"].endswith("repo")
        assert {"created_at", "updated_at"} <= set(row)

        single = await client.get(f"/api/projects/{pid}")
        assert single.status_code == 200
        assert single.json()["id"] == pid
        assert single.json()["name"] == "jsonproj"

        missing = await client.get("/api/projects/nope")
        assert missing.status_code == 404
