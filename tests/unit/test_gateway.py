"""WP-2.2 — model gateway: dispatch, redaction, retry, budget integration."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from girder.budget.guard import BudgetExceeded, BudgetGuard
from girder.config import ModelRole, NotifyConfig, Secrets, Settings
from girder.db import repo
from girder.db.models import AttemptStatus, RunStatus
from girder.fsm import current_status, transition_attempt
from girder.guard.redact import Redactor
from girder.models.gateway import Message, ModelError, ModelGateway
from girder.notify.notifier import Notifier

PRICE_IN_MTOK = 3.0
PRICE_OUT_MTOK = 15.0
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _role(role_name: str) -> ModelRole:
    return ModelRole(
        role=role_name,
        provider="openrouter",
        model="test/cheap-model",
        max_output_tokens=1000,
        price_in_per_mtok=PRICE_IN_MTOK,
        price_out_per_mtok=PRICE_OUT_MTOK,
    )


def _settings() -> Settings:
    return Settings(models={"roles": [_role("tier1"), _role("tier2"), _role("tier3")]})


def _ok_body(content: str | None = "ok") -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


class GatewayTransport(httpx.MockTransport):
    """Scriptable transport recording each request payload."""

    def __init__(self, responder: Any) -> None:
        self.requests: list[dict[str, Any]] = []
        self.calls = 0
        super().__init__(self._handle)
        self._responder = responder

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.requests.append(json.loads(request.content))
        return self._responder(self.calls)


def _gateway(
    db: Any, transport: GatewayTransport, *, notifier: Notifier | None = None
) -> ModelGateway:
    return ModelGateway(
        _settings(),
        Secrets(models_openrouter_api_key="sk-test-nonsecret"),
        db,
        Redactor(),
        BudgetGuard(db),
        notifier,
        transport=transport,
        retry_backoff_s=0,
    )


async def _seed_run(db: Any, *, cap: float = 5.0) -> str:
    project = await repo.create_project(db, "p", "/tmp/p")
    run = await repo.create_run(db, project.id, "intent", "branch", budget_cap_usd=cap)
    return run.id


async def _seed_attempt(db: Any, run_id: str) -> str:
    wave = await repo.create_wave(db, run_id, 0)
    from girder.db.models import TaskType

    task = await repo.create_task(db, wave.id, 0, "t", TaskType.CODE_CHANGE)
    attempt = await repo.create_attempt(db, task.id, "base")
    return attempt.id


async def test_success_path_redacts_persists_and_audits(db) -> None:  # type: ignore[no-untyped-def]
    transport = GatewayTransport(lambda n: httpx.Response(
        200, json=_ok_body(f"ok AWS key: {AWS_KEY}")
    ))
    run_id = await _seed_run(db)
    gw = _gateway(db, transport)
    try:
        resp = await gw.complete(
            "tier1",
            [Message(role="user", content="hello")],
            run_id=run_id,
        )
    finally:
        await gw.aclose()

    # Request payload: model id, max_tokens from role config, messages passed through.
    assert transport.calls == 1
    payload = transport.requests[0]
    assert payload["model"] == "test/cheap-model"
    assert payload["max_tokens"] == 1000
    assert payload["messages"] == [{"role": "user", "content": "hello"}]

    # Response content redacted before it reaches the caller.
    assert AWS_KEY not in (resp.content or "")
    assert "AKI***[REDACTED:" in (resp.content or "")
    assert resp.usage.prompt_tokens == 10
    assert resp.usage.completion_tokens == 5
    assert resp.finish_reason == "stop"
    assert resp.model_id == "test/cheap-model"
    assert resp.provider == "openrouter"
    assert resp.usage_row_id is not None

    # Redaction audit row exists; the raw value was never stored.
    redactions = await db.fetchall("SELECT source_field, pattern_matched FROM redaction_log")
    assert any(r["source_field"] == "model_response.content" for r in redactions)

    # token_usage row reconciled with actuals and cost.
    rows = await repo.list_token_usage_for_run(db, run_id)
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 10
    assert rows[0]["completion_tokens"] == 5
    assert rows[0]["cost_usd"] == pytest.approx(10 * 3e-6 + 5 * 15e-6)
    assert rows[0]["estimated_before_call"] > 0

    # Run spend updated.
    run = await repo.get_run(db, run_id)
    assert run is not None
    assert run.spend_usd == pytest.approx(10 * 3e-6 + 5 * 15e-6)

    # Audit events.
    events = await db.fetchall(
        "SELECT event_type FROM agent_events WHERE run_id = ?", (run_id,)
    )
    types = {r["event_type"] for r in events}
    assert "model_call_dispatch" in types
    assert "model_call_completed" in types


async def test_tool_calls_parsed_and_redacted(db) -> None:  # type: ignore[no-untyped-def]
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": "a.py"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }
    transport = GatewayTransport(lambda n: httpx.Response(200, json=body))
    run_id = await _seed_run(db)
    gw = _gateway(db, transport)
    try:
        resp = await gw.complete("tier1", [Message(role="user", content="do")], run_id=run_id)
    finally:
        await gw.aclose()
    assert resp.content is None
    assert len(resp.tool_calls) == 1
    tc = resp.tool_calls[0]
    assert tc.id == "call_1"
    assert tc.name == "read_file"
    assert json.loads(tc.arguments_json) == {"path": "a.py"}


async def test_retry_on_429_then_success(db) -> None:  # type: ignore[no-untyped-def]
    transport = GatewayTransport(
        lambda n: httpx.Response(429)
        if n == 1
        else httpx.Response(200, json=_ok_body("fine"))
    )
    run_id = await _seed_run(db)
    gw = _gateway(db, transport)
    try:
        resp = await gw.complete("tier1", [Message(role="user", content="x")], run_id=run_id)
    finally:
        await gw.aclose()
    assert transport.calls == 2
    assert resp.content == "fine"


async def test_retries_exhausted_fails_and_zeroes_usage_row(db) -> None:  # type: ignore[no-untyped-def]
    transport = GatewayTransport(lambda n: httpx.Response(500, text="boom"))
    run_id = await _seed_run(db)
    gw = _gateway(db, transport)
    try:
        with pytest.raises(ModelError, match="500"):
            await gw.complete("tier1", [Message(role="user", content="x")], run_id=run_id)
    finally:
        await gw.aclose()
    assert transport.calls == 3  # initial + 2 retries

    rows = await repo.list_token_usage_for_run(db, run_id)
    assert len(rows) == 1
    assert rows[0]["prompt_tokens"] == 0
    assert rows[0]["completion_tokens"] == 0
    assert rows[0]["cost_usd"] == 0.0

    events = await db.fetchall(
        "SELECT event_type FROM agent_events WHERE run_id = ?", (run_id,)
    )
    assert any(r["event_type"] == "model_call_failed" for r in events)


async def test_preflight_denial_never_dispatches(db) -> None:  # type: ignore[no-untyped-def]
    transport = GatewayTransport(lambda n: httpx.Response(200, json=_ok_body()))
    project = await repo.create_project(db, "p", "/tmp/p")
    run = await repo.create_run(db, project.id, "intent", "branch", budget_cap_usd=0.0000001)
    # seed an FSM state that has a budget_exhausted edge (draft does not).
    await db.execute("UPDATE runs SET status = ? WHERE id = ?",
                     (RunStatus.ACTIVE.value, run.id))
    await db.conn.commit()

    gw = _gateway(db, transport)
    try:
        with pytest.raises(BudgetExceeded):
            await gw.complete(
                "tier1", [Message(role="user", content="expensive")], run_id=run.id
            )
    finally:
        await gw.aclose()

    assert transport.calls == 0  # DoD: the deny path never dispatches
    assert await current_status(db, "run", run.id) == RunStatus.BUDGET_EXHAUSTED.value
    events = await db.fetchall(
        "SELECT event_type FROM agent_events WHERE run_id = ?", (run.id,)
    )
    assert any(r["event_type"] == "budget_preflight_denied" for r in events)


async def test_preflight_denial_freezes_attempt_and_notifies(db) -> None:  # type: ignore[no-untyped-def]
    transport = GatewayTransport(lambda n: httpx.Response(200, json=_ok_body()))
    notifier = Notifier(NotifyConfig(channels=[]), Secrets(), Redactor(), db=db)
    project = await repo.create_project(db, "p", "/tmp/p")
    run = await repo.create_run(db, project.id, "intent", "branch", budget_cap_usd=0.0000001)
    await db.execute("UPDATE runs SET status = ? WHERE id = ?",
                     (RunStatus.ACTIVE.value, run.id))
    await db.conn.commit()
    attempt_id = await _seed_attempt(db, run.id)
    await transition_attempt(db, attempt_id, AttemptStatus.RUNNING)

    gw = _gateway(db, transport, notifier=notifier)
    try:
        with pytest.raises(BudgetExceeded):
            await gw.complete(
                "tier1",
                [Message(role="user", content="expensive")],
                run_id=run.id,
                attempt_id=attempt_id,
            )
    finally:
        await gw.aclose()
    assert transport.calls == 0
    assert await current_status(db, "attempt", attempt_id) == AttemptStatus.BUDGET_FROZEN.value
    # channels=[] logs rather than sends, but still records.
    rows = await db.fetchall("SELECT channel FROM notifications_log")
    assert len(rows) == 1


async def test_unknown_role_raises(db) -> None:  # type: ignore[no-untyped-def]
    transport = GatewayTransport(lambda n: httpx.Response(200, json=_ok_body()))
    run_id = await _seed_run(db)
    gw = _gateway(db, transport)
    try:
        with pytest.raises(ModelError, match="unknown model role"):
            await gw.complete("tier9", [Message(role="user", content="x")], run_id=run_id)
    finally:
        await gw.aclose()
    assert transport.calls == 0


async def test_missing_api_key_raises(db) -> None:  # type: ignore[no-untyped-def]
    transport = GatewayTransport(lambda n: httpx.Response(200, json=_ok_body()))
    run_id = await _seed_run(db)
    gw = ModelGateway(
        _settings(),
        Secrets(),  # no keys at all
        db,
        Redactor(),
        BudgetGuard(db),
        transport=transport,
        retry_backoff_s=0,
    )
    try:
        with pytest.raises(ModelError, match="missing API key"):
            await gw.complete("tier1", [Message(role="user", content="x")], run_id=run_id)
    finally:
        await gw.aclose()
    assert transport.calls == 0
