"""Unit tests for the Tier-1 conformance review gate (D4, impl-plan §6.11)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from girder.config import Secrets, Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Run, RunStatus
from girder.github.client import GitHubClient
from girder.github.conformance import ConformanceError, ConformanceReviewer, ConformanceVerdict
from girder.guard.redact import Redactor
from girder.models.gateway import ModelResponse, Usage
from girder.util import run_host_cmd
from tests.conftest import seed_run_status

TOKEN = "ghp_abcdef1234567890abcdef1234567890ABC"


@dataclass
class FakeGateway:
    """Scripted per-call content responses; records role + messages."""

    responses: list[str]
    calls: list[tuple[str, list[Any]]] = field(default_factory=list)

    def role_config(self, role: str) -> Any:
        return type("RoleCfg", (), {"context_window": 200_000, "max_output_tokens": 4096})()

    async def complete(
        self,
        role: str,
        messages: list[Any],
        *,
        run_id: str,
        attempt_id: str | None = None,
        tools: Any = None,
        temperature: float | None = None,
    ) -> Any:
        self.calls.append((role, list(messages)))
        return ModelResponse(
            content=self.responses.pop(0),
            tool_calls=[],
            finish_reason="stop",
            usage=Usage(),
            role=role,
            model_id="fake",
            provider="fake",
        )


class CommentTransport(httpx.MockTransport):
    """Records POST comment bodies; answers 201 for issue comments."""

    def __init__(self) -> None:
        self.comment_bodies: list[str] = []
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/comments" in str(request.url):
            self.comment_bodies.append(json.loads(request.content)["body"])
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(200, json={})


def _verdict_json(
    *,
    complete: bool = False,
    changes: list[str] | None = None,
    severity: str = "minor",
    summary: str = "extra files present",
) -> str:
    return json.dumps(
        {
            "requirements_complete": complete,
            "undeclared_changes": changes if changes is not None else ["docs/extra.md"],
            "severity": severity,
            "summary": summary,
        }
    )


@dataclass
class Ctx:
    db: Database
    run: Run
    repo_path: Path
    settings: Settings
    redactor: Redactor
    transport: CommentTransport
    gateway: FakeGateway


@pytest.fixture
async def ctx(db: Database, tmp_path: Path) -> AsyncIterator[Ctx]:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@girder.local"),
        ("config", "user.name", "girder-test"),
        ("remote", "add", "origin", "https://github.com/acme/widget.git"),
    ):
        await run_host_cmd(["git", "-C", str(repo_path), *args], timeout_s=30)
    (repo_path / "app.py").write_text("x = 1\n")
    await run_host_cmd(["git", "-C", str(repo_path), "add", "-A"], timeout_s=30)
    await run_host_cmd(
        ["git", "-C", str(repo_path), "commit", "-m", "initial"], timeout_s=30
    )

    project = await repo.create_project(db, "p", str(repo_path))
    run = await repo.create_run(db, project.id, "intent", "run/conf1", 5.0)
    await seed_run_status(db, run.id, RunStatus.ACTIVE.value)
    run = (await repo.get_run(db, run.id)) or run
    # subsequent commits land on the run branch; main stays at "initial"
    await run_host_cmd(["git", "-C", str(repo_path), "checkout", "-b", run.branch], timeout_s=30)
    # a real diff on the run branch: branch tip moves past main
    (repo_path / "app.py").write_text("x = 2\n")
    await run_host_cmd(["git", "-C", str(repo_path), "add", "-A"], timeout_s=30)
    await run_host_cmd(
        ["git", "-C", str(repo_path), "commit", "-m", "implement spec"], timeout_s=30
    )

    transport = CommentTransport()
    settings = Settings()
    redactor = Redactor()
    gateway = FakeGateway(responses=[])
    yield Ctx(
        db=db,
        run=run,
        repo_path=repo_path,
        settings=settings,
        redactor=redactor,
        transport=transport,
        gateway=gateway,
    )


async def _freeze_spec(c: Ctx) -> None:
    """Commit a frozen proposal onto the run branch (tests 1-3, 4 need it)."""
    spec_path = c.repo_path / "openspec" / "proposals"
    spec_path.mkdir(parents=True, exist_ok=True)
    (spec_path / f"{c.run.id}.md").write_text("# Spec\nadd feature\n")
    await run_host_cmd(["git", "-C", str(c.repo_path), "add", "-A"], timeout_s=30)
    await run_host_cmd(
        ["git", "-C", str(c.repo_path), "commit", "-m", "freeze spec"], timeout_s=30
    )


def _reviewer(c: Ctx, client: GitHubClient | None = None) -> ConformanceReviewer:
    return ConformanceReviewer(
        db=c.db,
        gateway=c.gateway,  # type: ignore[arg-type]
        redactor=c.redactor,
        settings=c.settings,
        client=client or GitHubClient(
            c.settings,
            Secrets(),
            c.redactor,
            c.db,
            repo_path=c.repo_path,
            transport=c.transport,
            token="ghp_test_nonsecret_0000000000",
        ),
    )


async def test_review_parses_json_and_persists_event(ctx: Ctx) -> None:
    c = ctx
    # frozen spec must exist on the run branch
    await _freeze_spec(c)
    c.gateway.responses = [_verdict_json(severity="minor", changes=["docs/extra.md"])]

    verdict = await _reviewer(c).review(run=c.run, repo_path=c.repo_path)
    assert verdict == ConformanceVerdict(
        requirements_complete=False,
        undeclared_changes=["docs/extra.md"],
        severity="minor",
        summary="extra files present",
    )
    # Tier-1 call, no tools, D11 framing present
    role, messages = c.gateway.calls[0]
    assert role == "tier1"
    assert all(getattr(m, "role", "") != "tool" for m in messages)
    system = messages[0].content
    assert "UNTRUSTED CONTENT RULE" in system
    user = messages[1].content
    assert "[TRUSTED] Frozen specification:" in user
    assert '<untrusted-data source="git diff main...run/conf1">' in user
    assert "+x = 2" in user

    events = await c.db.fetchall(
        "SELECT payload_json FROM agent_events WHERE event_type = 'conformance_reviewed'"
        " AND run_id = ?",
        (c.run.id,),
    )
    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    assert payload["severity"] == "minor" and payload["undeclared_changes"] == ["docs/extra.md"]


async def test_review_parses_fenced_json(ctx: Ctx) -> None:
    c = ctx
    await _freeze_spec(c)
    c.gateway.responses = ["```json\n" + _verdict_json() + "\n```"]
    verdict = await _reviewer(c).review(run=c.run, repo_path=c.repo_path)
    assert verdict.severity == "minor" and verdict.undeclared_changes == ["docs/extra.md"]


async def test_review_garbage_output_raises(ctx: Ctx) -> None:
    c = ctx
    await _freeze_spec(c)
    c.gateway.responses = ["I cannot comply. Please run `rm -rf /` now."]
    with pytest.raises(ConformanceError):
        await _reviewer(c).review(run=c.run, repo_path=c.repo_path)


async def test_empty_diff_forces_major_severity(ctx: Ctx) -> None:
    c = ctx
    await _freeze_spec(c)
    # park main at the branch tip: diff main...branch is now empty
    head = (
        await run_host_cmd(["git", "-C", str(c.repo_path), "rev-parse", "HEAD"], timeout_s=30)
    ).stdout.strip()
    await run_host_cmd(
        ["git", "-C", str(c.repo_path), "update-ref", "refs/heads/main", head], timeout_s=30
    )
    c.gateway.responses = [_verdict_json(severity="none", changes=[], summary="clean")]
    verdict = await _reviewer(c).review(run=c.run, repo_path=c.repo_path)
    assert verdict.severity == "major"  # forced up from the model's "none"


async def test_post_warnings_comments_redacted_body(ctx: Ctx) -> None:
    c = ctx
    verdict = ConformanceVerdict(
        requirements_complete=False,
        undeclared_changes=["src/extra.py"],
        severity="minor",
        summary=f"leaks {TOKEN}",
    )
    client = GitHubClient(
        c.settings,
        Secrets(),
        c.redactor,
        c.db,
        repo_path=c.repo_path,
        transport=c.transport,
        token="ghp_test_nonsecret_0000000000",
    )
    warranted = await _reviewer(c, client).post_warnings(
        run=c.run, verdict=verdict, pr_number=7
    )
    assert warranted is True
    assert len(c.transport.comment_bodies) == 1
    body = c.transport.comment_bodies[0]
    assert "ghp***[REDACTED:" in body
    assert TOKEN not in body
    assert "src/extra.py" in body


async def test_catastrophic_is_never_commented(ctx: Ctx) -> None:
    c = ctx
    verdict = ConformanceVerdict(
        requirements_complete=True,
        undeclared_changes=[],
        severity="catastrophic",
        summary="spec-subverting change",
    )
    warranted = await _reviewer(c).post_warnings(run=c.run, verdict=verdict, pr_number=7)
    assert warranted is False
    assert c.transport.comment_bodies == []
