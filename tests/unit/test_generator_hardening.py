"""Hardening tests for the Tier-1 spec generator (2026-09 fix pass).

Covers the four production defenses added after a reasoning model parked a
run in spec_pending: output normalization (think blocks + leading prose),
actionable empty-content diagnosis, the bounded validation-feedback repair
loop, and the audit-intent prompt clause.
"""

from pathlib import Path

import pytest

from girder.config import Settings
from girder.db.models import Project, Run, RunStatus
from girder.models.gateway import Message, ModelResponse, Usage
from girder.specs.generator import SpecGenerationError, SpecGenerator, _normalize
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


class _ScriptedGateway:
    """Returns queued responses in order, recording every message list."""

    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
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
        return self.responses.pop(0)


def _response(content: str | None, finish_reason: str = "stop") -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=[],
        finish_reason=finish_reason,
        usage=Usage(),
        role="tier1",
        model_id="test/model",
        provider="openrouter",
    )


def _run() -> Run:
    return Run(
        id="run-1",
        project_id="proj-1",
        intent="Analyze if this project is fully and correctly implemented.",
        branch="run/run-1",
        status=RunStatus.SPEC_PENDING,
    )


def _project(tmp_path: Path) -> Project:
    return Project(id="proj-1", name="demo", repo_path=str(tmp_path))


def _gen(gateway: _ScriptedGateway, **settings_kwargs: object) -> SpecGenerator:
    settings = Settings(**{"specs": {"generation_attempts": 3, **settings_kwargs}})
    return SpecGenerator(gateway, settings)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "README.md").write_text("Demo project README contents.")
    return tmp_path


async def test_repair_loop_succeeds_on_second_attempt(repo: Path) -> None:
    """A rejected attempt is fed back with validator errors; attempt 2 wins."""
    bad = "I'll analyze the project systematically. Let me explore.\n"
    gateway = _ScriptedGateway([_response(bad), _response(_VALID_DOC)])
    result = await _gen(gateway).generate(
        run=_run(), project=_project(repo), repo_path=repo
    )
    assert parse_spec(result).title == "Add rate limiting"
    assert len(gateway.calls) == 2

    # The follow-up message wraps the rejected raw output as untrusted data.
    assert len(gateway.calls[1]) == 3
    repair = gateway.calls[1][-1]
    assert repair.role == "user"
    assert f"<previous-attempt>\n{bad}\n</previous-attempt>" in repair.content
    assert "failed structural validation" in repair.content
    assert (
        "- document must start with a '---' frontmatter delimiter line"
        in repair.content
    )
    assert "no commentary." in repair.content


async def test_attempts_exhausted_carries_last_raw_output_and_errors(repo: Path) -> None:
    """Exhaustion raises with the LAST raw output and aggregated errors."""
    first = "essay one, no delimiter"
    second = "essay two, still no delimiter"
    gateway = _ScriptedGateway([_response(first), _response(second)])
    with pytest.raises(SpecGenerationError) as exc_info:
        await _gen(gateway, generation_attempts=2).generate(
            run=_run(), project=_project(repo), repo_path=repo
        )
    assert exc_info.value.raw_output == second
    assert exc_info.value.errors == [
        "document must start with a '---' frontmatter delimiter line"
    ]
    assert len(gateway.calls) == 2


async def test_empty_content_raises_actionable_error_without_retry(repo: Path) -> None:
    """content=None (reasoning budget exhausted) diagnoses, never retries."""
    gateway = _ScriptedGateway([_response(None, finish_reason="length")])
    with pytest.raises(SpecGenerationError) as exc_info:
        await _gen(gateway).generate(
            run=_run(), project=_project(repo), repo_path=repo
        )
    message = "; ".join(exc_info.value.errors)
    assert "finish_reason='length'" in message
    assert "max_output_tokens" in message
    assert exc_info.value.raw_output is None
    assert len(gateway.calls) == 1


async def test_blank_content_raises_actionable_error_without_retry(repo: Path) -> None:
    """Whitespace-only content counts as no content (same diagnosis path)."""
    gateway = _ScriptedGateway([_response("   \n  ", finish_reason="length")])
    with pytest.raises(SpecGenerationError) as exc_info:
        await _gen(gateway).generate(
            run=_run(), project=_project(repo), repo_path=repo
        )
    assert "model produced no content" in "; ".join(exc_info.value.errors)
    assert len(gateway.calls) == 1


def test_normalize_strips_think_blocks() -> None:
    """<think>...</think> blocks are removed wherever they appear."""
    text = (
        "<think>reasoning about the repo</think>\n"
        f"{_VALID_DOC}<think>more reasoning</think>\n"
    )
    assert "<think>" not in _normalize(text)
    assert "---" in _normalize(text)


def test_normalize_rescues_leading_prose() -> None:
    """Conversational preamble above the frontmatter is dropped."""
    text = (
        "I'll analyze the Girder project systematically. Let me start by "
        "exploring the repository structure.\n\n"
        f"{_VALID_DOC}"
    )
    assert _normalize(text).startswith("---")
    assert "systematically" not in _normalize(text)


def test_normalize_leaves_valid_document_byte_identical() -> None:
    """An already-valid document passes through untouched."""
    assert _normalize(_VALID_DOC) == _VALID_DOC.strip() or _normalize(
        _VALID_DOC
    ) == _VALID_DOC.rstrip("\n")
    # Stronger: no rule fires at all.
    assert _normalize(_VALID_DOC) == _VALID_DOC.strip()


def test_normalize_leaves_text_without_delimiter_unchanged() -> None:
    """No '---' anywhere: returned as-is (validator error is honest)."""
    text = "just prose, no document"
    assert _normalize(text) == text


async def test_single_attempt_means_no_retry(repo: Path) -> None:
    """generation_attempts=1: one call, immediate raise on invalid output."""
    gateway = _ScriptedGateway([_response("no delimiter here")])
    with pytest.raises(SpecGenerationError):
        await _gen(gateway, generation_attempts=1).generate(
            run=_run(), project=_project(repo), repo_path=repo
        )
    assert len(gateway.calls) == 1


async def test_system_prompt_carries_audit_intent_clause(repo: Path) -> None:
    """The system prompt forbids conversational replies to audit intents."""
    gateway = _ScriptedGateway([_response(_VALID_DOC)])
    await _gen(gateway).generate(run=_run(), project=_project(repo), repo_path=repo)
    system = gateway.calls[0][0]
    assert OPENSPEC_TEMPLATE in system.content
    assert "never reply conversationally" in system.content
    assert "question or audit" in system.content
