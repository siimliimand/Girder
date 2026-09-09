"""Unit tests for the Tier-1 spec generator (impl-plan §6.9, D11)."""

from pathlib import Path

import pytest

from girder.budget.guard import BudgetExceeded, PreflightDecision
from girder.config import Settings
from girder.db.models import Project, Run, RunStatus
from girder.models.gateway import Message, ModelError, ModelResponse, Usage
from girder.specs.generator import SpecGenerationError, SpecGenerator
from girder.specs.validator import OPENSPEC_TEMPLATE, parse_spec

_VALID_DOC = """\
---
schema: girder.openspec/v1
title: Add rate limiting
intent: API abuse is throttled per client.
tasks:
  - id: rate-limiter
    title: Add token bucket middleware
    type: code_change
    scope_globs: ["src/api/**"]
    success_criteria:
      - "11th request within one minute returns 429"
    depends_on: []
---
Throttle details.
"""


class _FakeGateway:
    """Records messages, returns a canned response (or raises)."""

    def __init__(self, content: str | None = None, error: ModelError | None = None) -> None:
        self.content = content
        self.error = error
        self.calls: list[list[Message]] = []

    async def complete(
        self,
        role: str,
        messages: list[Message],
        run_id: str,
        *,
        attempt_id: str | None = None,
        tools: list[dict[str, object]] | None = None,
        temperature: float | None = None,
    ) -> ModelResponse:
        self.calls.append(messages)
        if self.error is not None:
            raise self.error
        return ModelResponse(
            content=self.content,
            tool_calls=[],
            finish_reason="stop",
            usage=Usage(),
            role=role,
            model_id="test/model",
            provider="openrouter",
        )


def _run() -> Run:
    return Run(
        id="run-1",
        project_id="proj-1",
        intent="Add rate limiting to the API",
        branch="run/run-1",
        status=RunStatus.SPEC_PENDING,
    )


def _project(tmp_path: Path) -> Project:
    return Project(id="proj-1", name="demo", repo_path=str(tmp_path))


def _gen(gateway: _FakeGateway) -> SpecGenerator:
    return SpecGenerator(gateway, Settings())


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "README.md").write_text("Demo project README contents.")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "api.py").write_text("# api\n")
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    return tmp_path


async def test_happy_path_returns_validated_doc(repo: Path) -> None:
    gateway = _FakeGateway(_VALID_DOC)
    result = await _gen(gateway).generate(
        run=_run(), project=_project(repo), repo_path=repo
    )
    assert result == _VALID_DOC.strip()
    parse_spec(result)  # already checked inside generate; keep the invariant explicit

    assert len(gateway.calls) == 1
    system, user = gateway.calls[0]
    assert system.role == "system"
    assert user.role == "user"
    assert OPENSPEC_TEMPLATE in system.content
    assert "UNTRUSTED CONTENT RULE" in system.content
    assert "never instructions" in system.content
    assert "depends_on" in system.content
    assert "genuinely independent tasks" in system.content

    assert "Add rate limiting to the API" in user.content
    assert '<untrusted-data source="README.md">' in user.content
    assert "Demo project README contents." in user.content
    assert '<untrusted-data source="file-listing">' in user.content
    assert "src/" in user.content
    assert "pyproject.toml" in user.content
    assert "<user-feedback>" not in user.content


async def test_readme_truncated_at_cap(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("A" * 20_000)
    gateway = _FakeGateway(_VALID_DOC)
    await _gen(gateway).generate(run=_run(), project=_project(tmp_path), repo_path=tmp_path)
    _, user = gateway.calls[0]
    assert "truncated by girder at 16000 characters" in user.content
    assert len(user.content) < 20_000


async def test_fences_are_stripped(repo: Path) -> None:
    gateway = _FakeGateway(f"```markdown\n{_VALID_DOC}\n```")
    result = await _gen(gateway).generate(
        run=_run(), project=_project(repo), repo_path=repo
    )
    assert result == _VALID_DOC.strip()


async def test_invalid_proposal_raises_with_errors(repo: Path) -> None:
    gateway = _FakeGateway("hello world, no frontmatter")
    with pytest.raises(SpecGenerationError) as exc_info:
        await _gen(gateway).generate(run=_run(), project=_project(repo), repo_path=repo)
    assert exc_info.value.errors


async def test_gateway_failure_raises_model_error_message(repo: Path) -> None:
    gateway = _FakeGateway(error=ModelError("boom"))
    with pytest.raises(SpecGenerationError) as exc_info:
        await _gen(gateway).generate(run=_run(), project=_project(repo), repo_path=repo)
    assert "boom" in exc_info.value.errors


async def test_budget_exceeded_wrapped_in_spec_generation_error(repo: Path) -> None:
    gateway = _FakeGateway(error=BudgetExceeded(PreflightDecision(
        allowed=False, est_in_tokens=1, est_out_tokens=1, est_cost_usd=1.0,
        remaining_usd=0.0, reason="run budget exhausted",
    )))
    with pytest.raises(SpecGenerationError) as exc_info:
        await _gen(gateway).generate(run=_run(), project=_project(repo), repo_path=repo)
    assert any("budget" in e for e in exc_info.value.errors)
    assert isinstance(exc_info.value.__cause__, BudgetExceeded)


async def test_feedback_block_only_when_present(repo: Path) -> None:
    gateway = _FakeGateway(_VALID_DOC)
    await _gen(gateway).generate(
        run=_run(), project=_project(repo), repo_path=repo, feedback="drop task 2"
    )
    _, user = gateway.calls[0]
    assert "<user-feedback>" in user.content
    assert "drop task 2" in user.content


async def test_missing_readme_omits_block(tmp_path: Path) -> None:
    gateway = _FakeGateway(_VALID_DOC)
    await _gen(gateway).generate(run=_run(), project=_project(tmp_path), repo_path=tmp_path)
    _, user = gateway.calls[0]
    assert 'source="README.md"' not in user.content
    assert '<untrusted-data source="file-listing">' in user.content


async def test_conventions_file_included_as_untrusted_data(tmp_path: Path) -> None:
    (tmp_path / "ARCHITECTURE.md").write_text("Hexagonal architecture; ports in src/ports/.")
    gateway = _FakeGateway(_VALID_DOC)
    await _gen(gateway).generate(run=_run(), project=_project(tmp_path), repo_path=tmp_path)
    _, user = gateway.calls[0]
    assert '<untrusted-data source="architecture-conventions">' in user.content
    assert "Hexagonal architecture; ports in src/ports/." in user.content


async def test_no_conventions_file_omits_block(tmp_path: Path) -> None:
    gateway = _FakeGateway(_VALID_DOC)
    await _gen(gateway).generate(run=_run(), project=_project(tmp_path), repo_path=tmp_path)
    _, user = gateway.calls[0]
    assert 'source="architecture-conventions"' not in user.content


async def test_conventions_truncated_at_cap(tmp_path: Path) -> None:
    (tmp_path / "CONTRIBUTING.md").write_text("B" * 20_000)
    gateway = _FakeGateway(_VALID_DOC)
    await _gen(gateway).generate(run=_run(), project=_project(tmp_path), repo_path=tmp_path)
    _, user = gateway.calls[0]
    assert "truncated by girder at 16000 characters" in user.content
