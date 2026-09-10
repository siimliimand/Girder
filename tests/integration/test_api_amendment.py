"""Integration: spec amendment resolution through the web console (impl-plan §10)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from girder.api.app import create_app
from girder.config import Secrets, Settings
from girder.db import repo
from girder.db.engine import Database, default_migrations_dir
from girder.db.models import RunStatus, TaskType
from girder.specs.freeze import approve_and_freeze
from girder.util import run_host_cmd
from tests.conftest import seed_run_status

pytestmark = pytest.mark.integration

PROPOSAL = """\
---
schema: girder.openspec/v1
title: Amendment api test
intent: Verify amendment endpoints end to end.
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


async def _git(cwd: Path, *args: str) -> str:
    result = await run_host_cmd(["git", "-C", str(cwd), *args], timeout_s=30)
    return result.stdout


@pytest.fixture
async def git_repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    await _git(repo_dir, "init", "-b", "main")
    await _git(repo_dir, "config", "user.email", "test@girder.local")
    await _git(repo_dir, "config", "user.name", "girder-test")
    (repo_dir / "README.md").write_text("repo\n")
    await _git(repo_dir, "add", "-A")
    await _git(repo_dir, "commit", "-m", "initial")
    return repo_dir


@dataclass
class Ctx:
    client: httpx.AsyncClient
    app: Any
    lifespan: Any  # keep a reference so gc doesn't run its __aexit__ early
    db: Database
    project_id: str
    run_id: str
    amendment_id: str


async def _setup(git_repo: Path, tmp_path: Path) -> Ctx:
    app = create_app(
        db_path=tmp_path / "web.db",
        settings=Settings(),
        secrets=Secrets(),
        migrations_dir=default_migrations_dir(),
        generator_factory=lambda: _NoopGenerator(),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=False
    )
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    db: Database = app.state.db
    project = await repo.create_project(db, "acme", str(git_repo))
    run = await repo.create_run(db, project.id, "intent", "run/amendapi", 5.0)
    await seed_run_status(db, run.id, RunStatus.SPEC_PENDING.value)
    fresh = await repo.get_run(db, run.id)
    assert fresh is not None
    await approve_and_freeze(
        db, project=project, run=fresh, proposal_text=PROPOSAL,
        repo_path=git_repo, worktree_base=tmp_path / "wt",
    )
    await seed_run_status(db, run.id, RunStatus.AWAITING_AMENDMENT.value)
    amendment = await repo.create_spec_amendment(db, run.id, "spec is wrong", "fix it")
    return Ctx(
        client=client, app=app, lifespan=lifespan, db=db,
        project_id=project.id, run_id=run.id, amendment_id=amendment.id,
    )


async def test_approve_endpoint(git_repo: Path, tmp_path: Path) -> None:
    ctx = await _setup(git_repo, tmp_path)
    try:
        resp = await ctx.client.post(
            f"/api/runs/{ctx.run_id}/amendments/{ctx.amendment_id}/approve"
        )
        assert resp.status_code == 303, resp.text
        meta = (await ctx.client.get(f"/api/runs/{ctx.run_id}")).json()
        assert meta["status"] == "active"
        listing = (await ctx.client.get(f"/api/runs/{ctx.run_id}/amendments")).json()
        assert listing["amendments"][0]["status"] == "approved"
        assert listing["amendments"][0]["new_spec_hash"] == meta["spec_hash"]
        blob = await _git(git_repo, "show", f"run/amendapi:openspec/proposals/{ctx.run_id}.md")
        assert "## Amendment:" in blob
        # run page shows no pending panel anymore
        page = await ctx.client.get(f"/runs/{ctx.run_id}")
        assert "Pending spec amendment" not in page.text
    finally:
        await ctx.lifespan.__aexit__(None, None, None)
        await ctx.client.aclose()


async def test_reject_endpoint_with_guidance(git_repo: Path, tmp_path: Path) -> None:
    ctx = await _setup(git_repo, tmp_path)
    try:
        resp = await ctx.client.post(
            f"/api/runs/{ctx.run_id}/amendments/{ctx.amendment_id}/reject",
            data={"guidance": "carry on"},
        )
        assert resp.status_code == 303, resp.text
        meta = (await ctx.client.get(f"/api/runs/{ctx.run_id}")).json()
        assert meta["status"] == "active"
        listing = (await ctx.client.get(f"/api/runs/{ctx.run_id}/amendments")).json()
        assert listing["amendments"][0]["status"] == "rejected"
        assert listing["amendments"][0]["guidance"] == "carry on"
    finally:
        await ctx.lifespan.__aexit__(None, None, None)
        await ctx.client.aclose()


async def test_abort_endpoint(git_repo: Path, tmp_path: Path) -> None:
    ctx = await _setup(git_repo, tmp_path)
    try:
        resp = await ctx.client.post(
            f"/api/runs/{ctx.run_id}/amendments/{ctx.amendment_id}/abort"
        )
        assert resp.status_code == 303, resp.text
        meta = (await ctx.client.get(f"/api/runs/{ctx.run_id}")).json()
        assert meta["status"] == "aborted"
        listing = (await ctx.client.get(f"/api/runs/{ctx.run_id}/amendments")).json()
        assert listing["amendments"][0]["status"] == "aborted"
    finally:
        await ctx.lifespan.__aexit__(None, None, None)
        await ctx.client.aclose()


async def test_unknown_amendment_404_and_double_resolve_409(
    git_repo: Path, tmp_path: Path
) -> None:
    ctx = await _setup(git_repo, tmp_path)
    try:
        missing = await ctx.client.post(f"/api/runs/{ctx.run_id}/amendments/nope/approve")
        assert missing.status_code == 404
        # wrong-run amendment id is also a 404
        other_run = await repo.create_run(ctx.db, ctx.project_id, "other", "run/other", 5.0)
        other = await repo.create_spec_amendment(ctx.db, other_run.id, "r", "c")
        resp = await ctx.client.post(
            f"/api/runs/{ctx.run_id}/amendments/{other.id}/approve"
        )
        assert resp.status_code == 404
        # resolve once, then again -> 409 with error fragment
        first = await ctx.client.post(
            f"/api/runs/{ctx.run_id}/amendments/{ctx.amendment_id}/reject"
        )
        assert first.status_code == 303
        again = await ctx.client.post(
            f"/api/runs/{ctx.run_id}/amendments/{ctx.amendment_id}/reject"
        )
        assert again.status_code == 409
        assert "already resolved" in again.text
    finally:
        await ctx.lifespan.__aexit__(None, None, None)
        await ctx.client.aclose()


async def test_pending_amendment_rendered_on_run_page(git_repo: Path, tmp_path: Path) -> None:
    ctx = await _setup(git_repo, tmp_path)
    try:
        page = await ctx.client.get(f"/runs/{ctx.run_id}")
        assert page.status_code == 200
        assert "Pending spec amendment" in page.text
        assert "spec is wrong" in page.text
        assert f"/api/runs/{ctx.run_id}/amendments/{ctx.amendment_id}/approve" in page.text
        panel = await ctx.client.get(f"/runs/{ctx.run_id}/panel")
        assert "Pending spec amendment" in panel.text
    finally:
        await ctx.lifespan.__aexit__(None, None, None)
        await ctx.client.aclose()


class _NoopGenerator:
    async def generate(self, **kwargs: Any) -> str:  # pragma: no cover - never dispatched
        return PROPOSAL


async def test_approve_endpoint_applies_scope_globs(git_repo: Path, tmp_path: Path) -> None:
    """§8.4: approving with scope_globs unions them into the task's globs."""
    ctx = await _setup(git_repo, tmp_path)
    try:
        db = ctx.db
        # attach a running task to the amendment (crash-window style: the run
        # is parked, the task never was)
        wave = await repo.get_or_create_wave0(db, ctx.run_id)
        task = await repo.create_task(
            db, wave.id, 1, "Do the thing", TaskType.CODE_CHANGE,
            scope_globs=["src/**"], spec_slice_md="",
        )
        await db.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task.id,))
        await db.conn.commit()
        await db.execute(
            "UPDATE spec_amendments SET task_id = ? WHERE id = ?",
            (task.id, ctx.amendment_id),
        )
        await db.conn.commit()

        resp = await ctx.client.post(
            f"/api/runs/{ctx.run_id}/amendments/{ctx.amendment_id}/approve",
            data={"scope_globs": "docs/**\nsrc/**"},
        )
        assert resp.status_code == 303, resp.text
        fresh = await repo.get_task(db, task.id)
        assert fresh is not None
        assert fresh.scope_globs == ["src/**", "docs/**"]
        stored = await repo.get_spec_amendment(db, ctx.amendment_id)
        assert stored is not None
        assert stored.status == "approved"
        assert stored.scope_globs == ["docs/**", "src/**"]
    finally:
        await ctx.lifespan.__aexit__(None, None, None)
        await ctx.client.aclose()
