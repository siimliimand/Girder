"""WS-04 — intent clarification loop (plan §8.5 / docs/improvements/
ws04-clarify-loop.md).

Covers the four mandated tests from the spec plus the generator's
clarification enrichment. No git, no network: generation is faked via
generator_factory, mirroring tests/unit/test_api.py.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from girder.api.app import create_app
from girder.config import Secrets, Settings
from girder.db import repo
from girder.db.engine import Database, default_migrations_dir
from girder.db.models import RunStatus
from girder.fsm import InvalidTransition, transition_run
from girder.specs.generator import (
    SpecGenerationError,
    SpecGenerator,
    _parse_clarification_questions,
)
from girder.util import new_id
from tests.conftest import seed_run_status
from tests.unit.test_api import GOLDEN

QUESTIONS = [
    "Which authentication backend should be used?",
    "Must existing sessions survive the migration?",
]


@dataclass
class _FakeGenerator:
    """SpecGeneratorLike with the clarification gate; records calls."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    clarify_calls: list[dict[str, Any]] = field(default_factory=list)
    questions: list[str] | None = None  # None -> generate_clarification_questions absent

    async def generate_clarification_questions(self, *, run: Any) -> list[str]:
        self.clarify_calls.append({"run_id": run.id, "intent": run.intent})
        return list(QUESTIONS)

    async def generate(
        self,
        *,
        run: Any,
        project: Any,
        repo_path: Path,
        feedback: str | None = None,
        clarification: dict[str, str] | None = None,
    ) -> str:
        self.calls.append(
            {
                "run_id": run.id,
                "intent": run.intent,
                "feedback": feedback,
                "clarification": clarification,
            }
        )
        return GOLDEN


def make_app(tmp_path: Path, fake: _FakeGenerator, settings: Settings | None = None) -> Any:
    return create_app(
        db_path=tmp_path / "api.db",
        settings=settings or Settings(),
        secrets=Secrets(),
        migrations_dir=default_migrations_dir(),
        generator_factory=lambda: fake,
    )


def make_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=False
    )


async def drain(app: Any) -> None:
    while app.state.background_tasks:
        await asyncio.gather(*list(app.state.background_tasks))
        await asyncio.sleep(0)


async def make_project(client: httpx.AsyncClient, tmp_path: Path) -> str:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(exist_ok=True)
    resp = await client.post("/api/projects", data={"name": "acme", "repo_path": str(repo_dir)})
    assert resp.status_code == 303, resp.text
    page = await client.get("/")
    for line in page.text.split("\n"):
        if "/projects/" in line and "acme" in line:
            return line.split('href="/projects/')[1].split('"')[0]
    raise AssertionError("project link not found on index")


async def open_session_row(db: Database, run_id: str) -> Any:
    return await db.fetchone(
        "SELECT * FROM clarification_sessions WHERE run_id = ? ORDER BY rowid DESC LIMIT 1",
        (run_id,),
    )


# ----------------------------------------------------------------- spec tests


async def test_clarify_true_parks_run_with_questions(tmp_path: Path) -> None:
    """Spec test 1: POST .../runs?clarify=true -> CLARIFYING, questions persisted."""
    fake = _FakeGenerator()
    app = make_app(tmp_path, fake)
    async with make_client(app) as client, app.router.lifespan_context(app):
        pid = await make_project(client, tmp_path)
        resp = await client.post(
            f"/api/projects/{pid}/runs",
            params={"clarify": "true"},
            data={"intent": "Make it better"},
        )
        assert resp.status_code == 303, resp.text
        location = resp.headers["location"]
        assert location.endswith("/clarify")
        rid = location.split("/runs/")[1].split("/")[0]
        run = await repo.get_run(app.state.db, rid)
        assert run is not None and run.status == RunStatus.CLARIFYING
        session = await open_session_row(app.state.db, rid)
        assert session is not None
        import json

        assert json.loads(session["questions_json"]) == QUESTIONS
        assert session["answers_json"] is None
        # The clarify page shows the questions.
        page = await client.get(f"/runs/{rid}/clarify")
        assert page.status_code == 200
        assert "Which authentication backend" in page.text
        # No spec was generated yet.
        assert fake.calls == []


async def test_clarify_answers_transition_and_enriched_generation(tmp_path: Path) -> None:
    """Spec test 2: POST /api/runs/{rid}/clarify with answers -> the run passes
    through DRAFT (clarification_answered) and spec generation receives the
    enriched intent."""
    fake = _FakeGenerator()
    app = make_app(tmp_path, fake)
    async with make_client(app) as client, app.router.lifespan_context(app):
        pid = await make_project(client, tmp_path)
        resp = await client.post(
            f"/api/projects/{pid}/runs", params={"clarify": "true"}, data={"intent": "Add login"}
        )
        rid = resp.headers["location"].split("/runs/")[1].split("/")[0]
        submit = await client.post(
            f"/api/runs/{rid}/clarify",
            data={"answers": ["OIDC", "Yes, via refresh tokens"]},
        )
        assert submit.status_code == 303, submit.text
        await drain(app)
        run = await repo.get_run(app.state.db, rid)
        assert run is not None
        # DRAFT is the transient hop mandated by §8.5; the run rests in
        # spec_pending with generation dispatched (mirrors create_run).
        assert run.status == RunStatus.SPEC_PENDING
        session = await open_session_row(app.state.db, rid)
        assert session is not None and session["answers_json"] is not None
        import json

        assert json.loads(session["answers_json"]) == ["OIDC", "Yes, via refresh tokens"]
        assert len(fake.calls) == 1
        intent = fake.calls[0]["intent"]
        assert intent.startswith("Add login")
        assert "Q: Which authentication backend should be used?" in intent
        assert "A: OIDC" in intent
        assert "A: Yes, via refresh tokens" in intent
        # The audit trail records the CLARIFYING -> DRAFT hop.
        events = await app.state.db.fetchall(
            "SELECT event_type FROM agent_events WHERE run_id = ?", (rid,)
        )
        kinds = {r["event_type"] for r in events}
        assert "clarification_requested" in kinds
        assert "clarification_answered" in kinds


async def test_clarify_answer_requires_all_answers(tmp_path: Path) -> None:
    fake = _FakeGenerator()
    app = make_app(tmp_path, fake)
    async with make_client(app) as client, app.router.lifespan_context(app):
        pid = await make_project(client, tmp_path)
        resp = await client.post(
            f"/api/projects/{pid}/runs", params={"clarify": "true"}, data={"intent": "Add login"}
        )
        rid = resp.headers["location"].split("/runs/")[1].split("/")[0]
        short = await client.post(f"/api/runs/{rid}/clarify", data={"answers": ["OIDC"]})
        assert short.status_code == 400
        blank = await client.post(f"/api/runs/{rid}/clarify", data={"answers": ["OIDC", "  "]})
        assert blank.status_code == 400
        run = await repo.get_run(app.state.db, rid)
        assert run is not None and run.status == RunStatus.CLARIFYING


async def test_fsm_rejects_illegal_edges_out_of_clarifying(db: Database) -> None:
    """Spec test 3: the only legal edge out of CLARIFYING is -> DRAFT."""
    from girder.db import repo as repo_mod

    project = await repo_mod.create_project(db, "fsm-clarify", "/tmp/fsm-clarify")
    run = await repo_mod.create_run(db, project.id, "intent", "run/x", 0.0)
    await seed_run_status(db, run.id, RunStatus.CLARIFYING.value)
    await transition_run(db, run.id, RunStatus.DRAFT, reason="clarification_answered")
    for illegal in (
        RunStatus.SPEC_PENDING,
        RunStatus.ACTIVE,
        RunStatus.ABORTED,
        RunStatus.FAILED,
        RunStatus.MERGED,
    ):
        await seed_run_status(db, run.id, RunStatus.CLARIFYING.value)
        with pytest.raises(InvalidTransition):
            await transition_run(db, run.id, illegal)
    # ...and DRAFT -> CLARIFYING is the only way in.
    await seed_run_status(db, run.id, RunStatus.DRAFT.value)
    await transition_run(db, run.id, RunStatus.CLARIFYING, reason="clarify")
    await seed_run_status(db, run.id, RunStatus.SPEC_PENDING.value)
    with pytest.raises(InvalidTransition):
        await transition_run(db, run.id, RunStatus.CLARIFYING)


async def test_default_flow_has_no_clarification_session(tmp_path: Path) -> None:
    """Spec test 4 (regression): clarify unset -> current behavior, byte for
    byte: no clarification session, straight to spec generation."""
    fake = _FakeGenerator()
    app = make_app(tmp_path, fake)
    async with make_client(app) as client, app.router.lifespan_context(app):
        pid = await make_project(client, tmp_path)
        resp = await client.post(f"/api/projects/{pid}/runs", data={"intent": "Ship it"})
        assert resp.status_code == 303
        assert resp.headers["location"].startswith("/runs/")
        rid = resp.headers["location"].split("/runs/")[1].split("/")[0]
        await drain(app)
        run = await repo.get_run(app.state.db, rid)
        assert run is not None and run.status == RunStatus.SPEC_PENDING
        assert await open_session_row(app.state.db, rid) is None
        assert fake.clarify_calls == []
        assert len(fake.calls) == 1
        assert fake.calls[0]["intent"] == "Ship it"
        # The clarify page redirects back to the run page.
        page = await client.get(f"/runs/{rid}/clarify", follow_redirects=False)
        assert page.status_code == 303


async def test_require_clarification_config_forces_the_loop(tmp_path: Path) -> None:
    fake = _FakeGenerator()
    settings = Settings(specs={"require_clarification": True})
    app = make_app(tmp_path, fake, settings)
    async with make_client(app) as client, app.router.lifespan_context(app):
        pid = await make_project(client, tmp_path)
        resp = await client.post(f"/api/projects/{pid}/runs", data={"intent": "Ship it"})
        rid = resp.headers["location"].split("/runs/")[1].split("/")[0]
        run = await repo.get_run(app.state.db, rid)
        assert run is not None and run.status == RunStatus.CLARIFYING
        assert fake.calls == []


# ------------------------------------------------------- generator unit tests


def _mk_generator(monkeypatch: pytest.MonkeyPatch, content: str | None) -> Any:
    class _Gw:
        async def complete(self, role: str, messages: list[Any], run_id: str | None = None) -> Any:
            return type("R", (), {"content": content, "finish_reason": "stop"})()

    return SpecGenerator(_Gw(), Settings())  # type: ignore[arg-type]


async def test_generator_questions_happy_path() -> None:
    gen = _mk_generator(None, '```json\n["q1?", "q2?", "q3?"]\n```')
    run = _fake_run()
    questions = await gen.generate_clarification_questions(run=run)
    assert questions == ["q1?", "q2?", "q3?"]


@pytest.mark.parametrize(
    "content",
    [
        '["only one question?"]',
        '["a", "b", "c", "d", "e"]',
        '["", "valid?"]',
        "not json at all",
        '{"questions": ["a?", "b?"]}',
    ],
)
async def test_generator_questions_rejects_malformed(content: str) -> None:
    gen = _mk_generator(None, content)
    with pytest.raises(SpecGenerationError):
        await gen.generate_clarification_questions(run=_fake_run())


async def test_generator_questions_no_content() -> None:
    gen = _mk_generator(None, None)
    with pytest.raises(SpecGenerationError, match="no content"):
        await gen.generate_clarification_questions(run=_fake_run())


def _fake_run() -> Any:
    from girder.db.models import Run

    return Run(
        id=new_id(),
        project_id="p",
        intent="Add login",
        branch="run/x",
        status=RunStatus.DRAFT,
        budget_cap_usd=1.0,
    )


def test_user_prompt_appends_clarification(tmp_path: Path) -> None:
    gen = _mk_generator(None, "[]")
    plain = gen._user_prompt("Add login", tmp_path, None)
    assert "A: OIDC" not in plain
    enriched = gen._user_prompt("Add login", tmp_path, None, {"Which backend?": "OIDC"})
    assert "Q: Which backend?\n  A: OIDC" in enriched
    # The answers are trusted requester input: inside <user-intent>, not an
    # untrusted-data block.
    user_intent = enriched.split("</user-intent>")[0]
    assert "<user-intent>\nAdd login\n\nClarifications" in user_intent
    assert user_intent.rstrip().endswith("A: OIDC")


def test_parse_clarification_questions_strips_fences() -> None:
    assert _parse_clarification_questions('```\n["a?", "b?"]\n```') == ["a?", "b?"]
