"""Integration: approve-and-freeze through the web console against a real
temp git repo (impl-plan §10, plan.md Phase 1 task 4)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from girder.api.app import create_app
from girder.config import Secrets, Settings
from girder.db.engine import Database, default_migrations_dir

pytestmark = pytest.mark.integration

PROPOSAL = """\
---
schema: girder.openspec/v1
title: Freeze test
intent: Verify the freeze path end to end.
tasks:
  - id: do-thing
    title: Do the thing
    type: code_change
    scope_globs: ["src/**"]
    success_criteria: ["the thing is done"]
    depends_on: []
---
Narrative.
"""


@dataclass
class _FakeGenerator:
    calls: int = 0

    async def generate(self, **kwargs: Any) -> str:
        self.calls += 1
        return PROPOSAL


async def _git(cwd: Path, *args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(cwd),
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    assert proc.returncode == 0


@pytest.fixture
async def git_repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    await _git(repo_dir, "init", "-b", "main")
    (repo_dir / "README.md").write_text("seed\n")
    await _git(repo_dir, "add", ".")
    await _git(repo_dir, "-c", "user.name=girder-test", "-c", "user.email=test@girder.local",
               "commit", "-m", "initial")
    return repo_dir


@dataclass
class Ctx:
    client: httpx.AsyncClient
    app: Any
    lifespan: Any  # keep a reference so gc doesn't run its __aexit__ early
    fake: _FakeGenerator
    project_id: str
    run_id: str


async def _setup(git_repo: Path, tmp_path: Path) -> Ctx:
    fake = _FakeGenerator()
    app = create_app(
        db_path=tmp_path / "web.db",
        settings=Settings(),
        secrets=Secrets(),
        migrations_dir=default_migrations_dir(),
        generator_factory=lambda: fake,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=False
    )
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    resp = await client.post(
        "/api/projects", data={"name": "acme", "repo_path": str(git_repo)}
    )
    assert resp.status_code == 303
    page = await client.get("/")
    pid = next(
        line.split('href="/projects/')[1].split('"')[0]
        for line in page.text.split("\n")
        if 'href="/projects/' in line and "acme" in line
    )
    resp = await client.post(f"/api/projects/{pid}/runs", data={"intent": "Ship it"})
    assert resp.status_code == 303
    rid = resp.headers["location"].rsplit("/", 1)[-1]
    # drain background generation
    while app.state.background_tasks:
        await asyncio.gather(*list(app.state.background_tasks))
        await asyncio.sleep(0)
    return Ctx(client=client, app=app, lifespan=lifespan, fake=fake, project_id=pid, run_id=rid)


async def test_approve_and_freeze_via_console(git_repo: Path, tmp_path: Path) -> None:
    ctx = await _setup(git_repo, tmp_path)
    try:
        rid = ctx.run_id
        meta = await ctx.client.get(f"/api/runs/{rid}")
        assert meta.json()["status"] == "spec_pending"
        assert meta.json()["has_proposal"] is True

        resp = await ctx.client.post(f"/api/runs/{rid}/approve")
        assert resp.status_code == 303, resp.text

        meta = await ctx.client.get(f"/api/runs/{rid}")
        body = meta.json()
        assert body["status"] == "spec_approved"
        assert body["spec_hash"] is not None

        page = await ctx.client.get(f"/runs/{rid}")
        assert page.status_code == 200
        assert "frozen" in page.text
        assert body["spec_hash"][:12] in page.text

        # second approve must be rejected
        again = await ctx.client.post(f"/api/runs/{rid}/approve")
        assert again.status_code == 400
        assert "already frozen" in again.text
    finally:
        await ctx.lifespan.__aexit__(None, None, None)
        await ctx.client.aclose()


async def test_approve_without_proposal_rejected(git_repo: Path, tmp_path: Path) -> None:
    ctx = await _setup(git_repo, tmp_path)
    try:
        rid = ctx.run_id
        # clear the proposal: generation "hasn't landed" for approve purposes
        db: Database = ctx.app.state.db
        await db.execute("UPDATE runs SET proposal_md = NULL WHERE id = ?", (rid,))
        await db.conn.commit()
        resp = await ctx.client.post(f"/api/runs/{rid}/approve")
        assert resp.status_code == 400
        assert "no proposal" in resp.text
    finally:
        await ctx.lifespan.__aexit__(None, None, None)
        await ctx.client.aclose()
