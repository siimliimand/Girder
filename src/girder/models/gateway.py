"""Thin model client — impl-plan §6.8 / D1.

No SDK, no LangChain: one ``httpx`` call per model invocation. The order of
operations inside :meth:`ModelGateway.complete` follows impl-plan §6.8 exactly:

estimate -> persist pre-dispatch row -> dispatch -> redact response ->
reconcile spend -> tripwire -> audit event. Responses pass the redactor
before they reach the caller, logs, or SQLite (§8.6).

Two transports share that pipeline: OpenAI-compatible chat-completions
(openai / openrouter) and the Anthropic Messages API. Only the request
shaping and response parsing differ per provider — retry, redaction and
budget wiring are identical. Base URLs follow one convention: the versioned
API root (``…/v1``), with the resource appended per provider
(``/chat/completions`` vs ``/messages``).
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from typing import Any

import httpx

from girder.budget.guard import BudgetExceeded, BudgetGuard
from girder.config import ModelRole, Secrets, Settings
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import AttemptStatus, RunStatus
from girder.fsm import InvalidTransition, transition_attempt, transition_run
from girder.guard.redact import Redactor, redact_and_log
from girder.notify.notifier import Notifier

DEFAULT_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}

_SECRET_FIELD = {
    "openrouter": "models_openrouter_api_key",
    "openai": "models_openai_api_key",
    "anthropic": "models_anthropic_api_key",
}

_MAX_HTTP_ATTEMPTS = 3  # initial + 2 retries (impl-plan §1: jittered backoff on 429/5xx, max 3)


class ModelError(RuntimeError):
    """A model call failed (HTTP error, retries exhausted, or malformed body).

    Messages carry status codes / exception class names — never raw secrets.
    """


@dataclass(slots=True)
class Message:
    role: str  # system | user | assistant | tool
    content: str


@dataclass(slots=True)
class ModelToolCall:
    id: str
    name: str
    arguments_json: str


@dataclass(slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(slots=True)
class ModelResponse:
    content: str | None
    tool_calls: list[ModelToolCall]
    finish_reason: str | None
    usage: Usage
    role: str
    model_id: str
    provider: str
    usage_row_id: int | None = None


class ModelGateway:
    def __init__(
        self,
        settings: Settings,
        secrets: Secrets,
        db: Database,
        redactor: Redactor,
        budget: BudgetGuard,
        notifier: Notifier | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_backoff_s: float = 0.5,
    ) -> None:
        self.settings = settings
        self.secrets = secrets
        self.db = db
        self.redactor = redactor
        self.budget = budget
        self.notifier = notifier
        # retry_backoff_s exists so tests can zero the jittered backoff.
        self.retry_backoff_s = retry_backoff_s
        self._client = httpx.AsyncClient(
            transport=transport, timeout=settings.models.request_timeout_s
        )

    def role_config(self, role: str) -> ModelRole:
        """Resolve a role name against the registry; unknown -> ModelError."""
        for cfg in self.settings.models.roles:
            if cfg.role == role:
                return cfg
        raise ModelError(f"unknown model role: {role!r}")

    def _api_key(self, provider: str) -> str:
        field_name = _SECRET_FIELD.get(provider)
        key = self.secrets.get(field_name) if field_name else None
        if not key:
            raise ModelError(f"missing API key for provider {provider!r} (secrets.{field_name})")
        return key

    def _headers(self, provider: str, key: str) -> dict[str, str]:
        if provider == "anthropic":
            return {"x-api-key": key, "anthropic-version": "2023-06-01"}
        return {"Authorization": f"Bearer {key}"}

    async def complete(
        self,
        role: str,
        messages: list[Message],
        *,
        run_id: str,
        attempt_id: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
    ) -> ModelResponse:
        """One chat completion: estimate, dispatch, redact, reconcile (§6.8).

        On pre-flight denial the call is never dispatched: the run is routed to
        budget_exhausted, the attempt (if any) to budget_frozen, a
        ``budget_preflight_denied`` event is recorded, and BudgetExceeded is
        raised. When the post-call tripwire fires the run is likewise routed
        to budget_exhausted and notified, but the already-paid-for response is
        still returned — the next pre-flight will deny.
        """
        # 1. Role config + API key.
        cfg = self.role_config(role)
        key = self._api_key(cfg.provider)

        # 2. Run lookup + spend snapshot.
        run = await repo.get_run(self.db, run_id)
        if run is None:
            raise ModelError(f"run {run_id!r} not found")
        snapshot_spend = run.spend_usd

        # 3. Pre-flight; denial aborts the dispatch (§6.8 step 1).
        prompt_chars = sum(len(m.content) for m in messages)
        decision = self.budget.preflight(cfg, prompt_chars, run)
        if not decision.allowed:
            await repo.insert_event(
                self.db,
                "budget_preflight_denied",
                {
                    "role": role,
                    "model": cfg.model,
                    "est_in_tokens": decision.est_in_tokens,
                    "est_out_tokens": decision.est_out_tokens,
                    "est_cost_usd": decision.est_cost_usd,
                    "remaining_usd": decision.remaining_usd,
                    "prompt_chars": prompt_chars,
                    "reason": decision.reason,
                },
                run_id=run_id,
                attempt_id=attempt_id,
            )
            if attempt_id:
                try:
                    await transition_attempt(self.db, attempt_id, AttemptStatus.BUDGET_FROZEN)
                except InvalidTransition:
                    pass
            try:
                await transition_run(self.db, run_id, RunStatus.BUDGET_EXHAUSTED,
                                    reason="preflight_denied")
            except InvalidTransition:
                pass
            if self.notifier is not None:
                await self.notifier.notify(
                    "error",
                    "Girder: budget exhausted — call not dispatched",
                    decision.reason or "pre-flight estimate exceeds remaining budget",
                    run_id=run_id,
                )
            raise BudgetExceeded(decision)

        # 4. Persist the pre-dispatch estimate row + dispatch audit event.
        usage_row_id = await repo.insert_token_usage(
            self.db,
            run_id=run_id,
            attempt_id=attempt_id,
            model_role=role,
            model_id=cfg.model,
            estimated_before_call=decision.est_cost_usd,
        )
        await repo.insert_event(
            self.db,
            "model_call_dispatch",
            {
                "role": role,
                "model": cfg.model,
                "est_in_tokens": decision.est_in_tokens,
                "est_out_tokens": decision.est_out_tokens,
                "est_cost_usd": decision.est_cost_usd,
                "prompt_chars": prompt_chars,
            },
            run_id=run_id,
            attempt_id=attempt_id,
        )

        # 5. Dispatch with jittered-backoff retries on 429/5xx/transport errors.
        base_url = cfg.base_url or DEFAULT_BASE_URLS[cfg.provider]
        headers = self._headers(cfg.provider, key)
        if cfg.provider == "anthropic":
            payload = self._anthropic_payload(cfg, messages, tools, temperature)
            endpoint = f"{base_url}/messages"
        else:
            payload = {
                "model": cfg.model,
                "messages": [{"role": m.role, "content": m.content} for m in messages],
                "max_tokens": cfg.max_output_tokens,
            }
            if tools is not None:
                payload["tools"] = tools
            if temperature is not None:
                payload["temperature"] = temperature
            endpoint = f"{base_url}/chat/completions"

        data: dict[str, Any] | None = None
        failure: str | None = None
        for attempt in range(1, _MAX_HTTP_ATTEMPTS + 1):
            try:
                resp = await self._client.post(endpoint, json=payload, headers=headers)
                if resp.status_code == 429 or resp.status_code >= 500:
                    failure = f"HTTP {resp.status_code}"
                    if attempt < _MAX_HTTP_ATTEMPTS:
                        await self._backoff(attempt)
                        continue
                    break
                if resp.status_code >= 400:
                    failure = f"HTTP {resp.status_code}"
                    break
                data = resp.json()
                break
            except httpx.TransportError as exc:
                failure = type(exc).__name__
                if attempt < _MAX_HTTP_ATTEMPTS:
                    await self._backoff(attempt)
                    continue
                break

        if data is None:
            await self._fail(usage_row_id, run_id, attempt_id, failure or "unknown error")
            raise ModelError(f"model call failed after retries: {failure}")

        # 6. Parse the provider response shape (normalized to the OpenAI
        # fields the rest of the pipeline — redaction, reconcile — consumes).
        try:
            if cfg.provider == "anthropic":
                content, tool_calls_raw, usage, finish_reason, resp_role = (
                    self._parse_anthropic(data)
                )
            else:
                choices = data.get("choices") or []
                if not choices:
                    raise ValueError("empty choices")
                message = choices[0].get("message") or {}
                content = message.get("content")
                tool_calls_raw = message.get("tool_calls") or []
                usage_raw = data.get("usage") or {}
                usage = Usage(
                    prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
                    completion_tokens=int(usage_raw.get("completion_tokens") or 0),
                )
                finish_reason = choices[0].get("finish_reason")
                resp_role = str(message.get("role") or "assistant")
        except (AttributeError, TypeError, ValueError) as exc:
            await self._fail(usage_row_id, run_id, attempt_id, f"malformed response: {exc}")
            raise ModelError(f"malformed model response: {exc}") from exc

        # 7. Redact before the response reaches caller/logs/SQLite (§8.6).
        # A null content stays null in the response but still passes through
        # the redactor (as "") so the pipeline is uniform.
        redacted_content = (
            await redact_and_log(
                self.redactor,
                content,
                source_field="model_response.content",
                db=self.db,
                attempt_id=attempt_id,
            )
            if content is not None
            else None
        )
        tool_calls: list[ModelToolCall] = []
        for tc in tool_calls_raw:
            fn = tc.get("function") or {}
            redacted_args = await redact_and_log(
                self.redactor,
                str(fn.get("arguments") or ""),
                source_field="model_response.tool_call",
                db=self.db,
                attempt_id=attempt_id,
            )
            tool_calls.append(
                ModelToolCall(
                    id=str(tc.get("id") or ""),
                    name=str(fn.get("name") or ""),
                    arguments_json=redacted_args,
                )
            )

        # 9. Reconcile spend with actuals; 10. tripwire re-check.
        outcome = await self.budget.reconcile(
            usage_row_id,
            run_id,
            cfg,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            projected_total=snapshot_spend + decision.est_cost_usd,
        )
        if outcome.over_budget:
            await repo.insert_event(
                self.db,
                "budget_tripwire",
                {
                    "role": role,
                    "model": cfg.model,
                    "cost_usd": outcome.actual_cost_usd,
                    "run_spend_usd": outcome.run_spend_usd,
                    "budget_cap_usd": run.budget_cap_usd,
                },
                run_id=run_id,
            )
            try:
                await transition_run(self.db, run_id, RunStatus.BUDGET_EXHAUSTED,
                                    reason="post_call_tripwire")
            except InvalidTransition:
                pass
            if self.notifier is not None:
                await self.notifier.notify(
                    "error",
                    "Girder: budget cap exceeded (post-call tripwire)",
                    f"run spend ${outcome.run_spend_usd:.6f} exceeds cap",
                    run_id=run_id,
                )

        # 11. Completion audit event; 12. return the redacted response.
        await repo.insert_event(
            self.db,
            "model_call_completed",
            {
                "role": role,
                "model": cfg.model,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "cost_usd": outcome.actual_cost_usd,
                "over_budget": outcome.over_budget,
            },
            run_id=run_id,
            attempt_id=attempt_id,
        )
        return ModelResponse(
            content=redacted_content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=usage,
            role=resp_role,
            model_id=cfg.model,
            provider=cfg.provider,
            usage_row_id=usage_row_id,
        )

    # -------------------------------------------------- anthropic transport

    def _anthropic_payload(
        self,
        cfg: ModelRole,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
    ) -> dict[str, Any]:
        """Shape the Anthropic Messages API request (non-streaming).

        Base URL convention (shared with the OpenAI branches): the versioned
        API root, ``https://api.anthropic.com/v1`` by default; the endpoint is
        ``{base}/messages``. ``max_tokens`` is REQUIRED by this API — the
        role's configured ``max_output_tokens`` is used.
        """
        system_parts = [m.content for m in messages if m.role == "system"]
        convo: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "tool":
                # The internal Message carries no tool_use id (results are
                # plain text in this pipeline), so the block's id is empty —
                # the API pairing key only matters for native tool round-trips.
                convo.append(
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "",
                                     "content": m.content}],
                    }
                )
            else:
                convo.append(
                    {"role": m.role, "content": [{"type": "text", "text": m.content}]}
                )
        payload: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_output_tokens,
            "messages": convo,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if tools is not None:
            payload["tools"] = [
                {
                    "name": t.get("function", {}).get("name", ""),
                    "description": t.get("function", {}).get("description", ""),
                    "input_schema": t.get("function", {}).get("parameters", {}),
                }
                for t in tools
            ]
        if temperature is not None:
            payload["temperature"] = temperature
        return payload

    def _parse_anthropic(
        self, data: dict[str, Any]
    ) -> tuple[str | None, list[dict[str, Any]], Usage, str | None, str]:
        """Parse a Messages API response into the OpenAI-shaped fields the
        shared pipeline consumes: (content, tool_calls, usage, finish_reason,
        role). ``tool_use`` blocks are converted to OpenAI tool-call dicts so
        the redaction loop below stays provider-agnostic; ``stop_reason`` is
        mapped onto OpenAI finish reasons for loop control."""
        blocks = data.get("content") or []
        if not isinstance(blocks, list):
            raise ValueError("content is not a list")
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in blocks:
            if block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": str(block.get("id") or ""),
                        "function": {
                            "name": str(block.get("name") or ""),
                            "arguments": json.dumps(block.get("input") or {}),
                        },
                    }
                )
        usage_raw = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("input_tokens") or 0),
            completion_tokens=int(usage_raw.get("output_tokens") or 0),
        )
        stop = data.get("stop_reason")
        finish = {
            "tool_use": "tool_calls",
            "end_turn": "stop",
            "stop_sequence": "stop",
            "max_tokens": "length",
        }.get(str(stop), str(stop) if stop else None)
        return (
            "\n".join(text_parts) if text_parts else None,
            tool_calls,
            usage,
            finish,
            "assistant",
        )

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(self.retry_backoff_s * 2 ** (attempt - 1) + random.uniform(0, 0.25))

    async def _fail(
        self, usage_row_id: int, run_id: str, attempt_id: str | None, error_text: str
    ) -> None:
        """Failure path (§6.8 step 8): zero the estimate row, audit redacted."""
        await repo.update_token_usage_actual(
            self.db, usage_row_id, prompt_tokens=0, completion_tokens=0, cost_usd=0.0
        )
        redacted = await redact_and_log(
            self.redactor,
            error_text,
            source_field="model_response.error",
            db=self.db,
            attempt_id=attempt_id,
        )
        await repo.insert_event(
            self.db,
            "model_call_failed",
            {"error": redacted[:2000]},
            run_id=run_id,
            attempt_id=attempt_id,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
