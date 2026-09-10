"""FastAPI application factory for the approval web console (impl-plan §10).

Server-rendered Jinja2 pages plus a handful of JSON endpoints. The only
privileged side effects are spec generation dispatch (background task) and
approve-and-freeze — both on explicit user action.

The spec generator is injected structurally via :class:`SpecGeneratorLike` so
tests can supply fakes without network access; the default factory imports
:class:`girder.specs.generator.SpecGenerator` lazily inside the function body
so a partially-written sibling module can never break app construction.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from girder import fsm
from girder.budget.guard import BudgetGuard
from girder.config import Secrets, Settings, load_secrets
from girder.db import repo
from girder.db.engine import Database, default_migrations_dir
from girder.db.models import Project, Run
from girder.guard.redact import Redactor
from girder.models.gateway import ModelGateway
from girder.notify.notifier import Notifier

log = logging.getLogger(__name__)


class SpecGeneratorLike(Protocol):
    async def generate(
        self,
        *,
        run: Run,
        project: Project,
        repo_path: Path,
        feedback: str | None = None,
    ) -> str: ...


_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(
    *,
    db_path: Path,
    settings: Settings | None = None,
    secrets: Secrets | None = None,
    migrations_dir: Path | None = None,
    generator_factory: Callable[[], SpecGeneratorLike] | None = None,
) -> FastAPI:
    """Build the console app; opens (and migrates) its database on startup."""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = await Database.open(
            db_path, migrations_dir=migrations_dir or default_migrations_dir()
        )
        # §6.3 boot-time self-test: fail fast if the edge tables and the
        # guarded transition engine ever disagree.
        await fsm.self_test()
        secrets_ = secrets or load_secrets()
        redactor = Redactor(secrets_.redaction_secret_env_names)
        budget = BudgetGuard(db)
        gateway = ModelGateway(settings, secrets_, db, redactor, budget)
        notifier = Notifier(settings.notify, secrets_, redactor, db=db)

        def _default_factory() -> SpecGeneratorLike:
            from girder.specs.generator import SpecGenerator  # lazy: mid-write safe

            return SpecGenerator(gateway, settings)

        app.state.db = db
        app.state.settings = settings
        app.state.secrets = secrets_
        app.state.redactor = redactor
        app.state.budget = budget
        app.state.gateway = gateway
        app.state.notifier = notifier
        app.state.create_spec_generator = generator_factory or _default_factory
        app.state.background_tasks = set()
        try:
            yield
        finally:
            await gateway.aclose()
            tasks = [t for t in app.state.background_tasks if not t.done()]
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            app.state.background_tasks.clear()
            await db.close()

    app = FastAPI(title="Girder", lifespan=lifespan)
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent / "static")),
        name="static",
    )

    from girder.api.metrics import router as metrics_router
    from girder.api.routes import router

    app.include_router(router)
    # /metrics (WP 12.3) is registered here rather than in routes.py: the
    # console router is a contention point for parallel workstreams, and the
    # metrics endpoint is an operational side-channel, not console surface.
    app.include_router(metrics_router)
    return app


def dispatch_generation(app: FastAPI, run_id: str, feedback: str | None) -> None:
    """Fire-and-forget spec generation, tracked for clean shutdown (§10)."""
    task = asyncio.create_task(_generate(app, run_id, feedback))
    app.state.background_tasks.add(task)
    task.add_done_callback(app.state.background_tasks.discard)


async def _generate(app: FastAPI, run_id: str, feedback: str | None) -> None:
    """Generate a proposal and persist it; failures land in agent_events."""
    db: Database = app.state.db
    try:
        run = await repo.get_run(db, run_id)
        if run is None:
            log.error("generation for missing run %s", run_id)
            return
        project = await repo.get_project(db, run.project_id)
        if project is None:
            log.error("generation for run %s: missing project", run_id)
            return
        generator = app.state.create_spec_generator()
        text = await generator.generate(
            run=run,
            project=project,
            repo_path=Path(project.repo_path),
            feedback=feedback,
        )
        await repo.update_run_fields(db, run_id, proposal_md=text)
        await repo.insert_event(
            db,
            "spec_generation_finished",
            {"chars": len(text), "feedback": feedback},
            run_id=run_id,
        )
    except Exception as exc:
        log.exception("spec generation failed for run %s", run_id)
        payload: dict[str, Any] = {"error": str(exc)[:2000]}
        # Keep the rejected model output for post-mortem (getattr: older
        # SpecGenerationError versions may not carry it). The raw content
        # already passed the gateway's redaction layer before returning.
        raw = getattr(exc, "raw_output", None)
        if raw:
            if len(raw) > 20_000:
                raw = raw[:20_000] + "\n[...truncated by girder at 20000 characters]"
            payload["raw_output"] = raw
        await repo.insert_event(db, "spec_generation_failed", payload, run_id=run_id)


def templates() -> Jinja2Templates:
    return _TEMPLATES


def render(
    request: Request, name: str, context: dict[str, Any], status_code: int = 200
) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request=request, name=name, context=context, status_code=status_code
    )
