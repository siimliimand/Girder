"""The agent turn loop (impl-plan §6.10).

Conversation shape: because ``Message`` is plain role/content strings (no
native tool-role messages), the loop maintains

    [system invariants, user task brief (+ trusted guidance)]

and after each model response appends ONE assistant message (its content, or
a compact textual rendering of the tool calls it made) followed by ONE user
message holding every tool result for that turn, each wrapped by
``prompts.wrap_tool_result`` — so tool output is uniformly framed as
untrusted data (§7).

The loop never enforces budget policy itself: exceptions from
``gateway.complete`` (e.g. ``BudgetExceeded`` from pre-flight) propagate to
the caller. What it owns is turn budget, wall-clock deadline, compaction
(§8.5), and terminal-tool outcomes.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from girder.agent import prompts
from girder.agent.context import CompactionStats, compact, should_compact
from girder.agent.tools import TOOL_SCHEMAS, ToolExecResult, ToolRegistry
from girder.config import LimitsConfig
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Attempt, Task
from girder.guard.redact import Redactor
from girder.guard.scope import TaskScopes
from girder.models.gateway import Message, ModelGateway, ModelToolCall
from girder.sandbox.engine import SandboxEngine

# Recent tail preserved across compaction so the loop stays coherent (§8.5).
_TAIL_AFTER_COMPACTION = 6

_NO_TOOL_NUDGE = (
    "You must act via tools; call mark_task_complete when the task is done "
    "(or request_spec_amendment if the frozen spec cannot be satisfied)."
)


@dataclass(frozen=True)
class AttemptOutcome:
    status: str  # "succeeded" | "failed" | "timeout" | "amendment_requested"
    summary: str | None
    failure_reason: str | None
    turns_used: int
    amendment: tuple[str, str] | None  # (reason, suggested_change)


def _render_tool_calls(
    calls: list[tuple[str, dict[str, object], ToolExecResult]],
) -> str:
    inner = ", ".join(f"{name}({json.dumps(args, default=str)})" for name, args, _ in calls)
    return f"[tool calls this turn: {inner}]"


def _source_for(name: str, args: dict[str, object]) -> str:
    """Conversation-visible provenance tag: tool plus its primary argument."""
    hint = ""
    if name == "run_command":
        hint = str(args.get("cmd", ""))[:80]
    else:
        hint = str(args.get("path") or args.get("glob") or args.get("regex") or "")
    return f"{name}:{hint}" if hint else name


class AgentRuntime:
    """Drives one attempt: model turns, tool execution, compaction, outcome."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        sandbox: SandboxEngine,
        container: str,
        scopes: TaskScopes,
        limits: LimitsConfig,
        redactor: Redactor,
        db: Database,
        run_id: str,
        attempt: Attempt,
        task: Task,
        model_role: str = "tier2",
    ) -> None:
        self.gateway = gateway
        self.limits = limits
        self.db = db
        self.run_id = run_id
        self.attempt = attempt
        self.task = task
        self.model_role = model_role
        self._context_window = gateway.role_config(model_role).context_window
        self._registry = ToolRegistry(
            sandbox=sandbox,
            container=container,
            scopes=scopes,
            limits=limits,
            redactor=redactor,
            db=db,
            attempt_id=attempt.id,
            run_id=run_id,
            task=task,
        )

    async def execute_attempt(
        self,
        *,
        spec_slice: str,
        guidance: str | None = None,
        deadline_s: float | None = None,
    ) -> AttemptOutcome:
        """Run the turn loop to a terminal tool, the turn cap, or the deadline.

        ``deadline_s`` is an absolute ``asyncio.get_running_loop().time()``
        value; per-turn model headroom is whatever remains when the turn
        starts. Killing the container remains the caller's job.
        """
        messages = [
            Message(
                role="system",
                content=prompts.build_system_prompt(task=self.task, spec_slice=spec_slice),
            ),
            Message(
                role="user",
                content=prompts.build_task_message(
                    task=self.task, spec_slice=spec_slice, guidance=guidance
                ),
            ),
        ]
        loop = asyncio.get_running_loop()
        turns_used = 0
        while turns_used < self.limits.attempt_max_turns:
            if deadline_s is not None and loop.time() >= deadline_s:
                return await self._finish(
                    "timeout", None, "wall-clock deadline exhausted", turns_used
                )
            turns_used += 1
            await repo.insert_event(
                self.db,
                "turn_start",
                {"turn": turns_used, "messages": len(messages)},
                run_id=self.run_id,
                attempt_id=self.attempt.id,
            )

            response = await self.gateway.complete(
                self.model_role,
                messages,
                tools=TOOL_SCHEMAS,
                run_id=self.run_id,
                attempt_id=self.attempt.id,
            )

            if not response.tool_calls:
                # No action: nudge and count the turn; cap handled by the loop.
                messages.append(Message(role="assistant", content=response.content or ""))
                messages.append(Message(role="user", content=_NO_TOOL_NUDGE))
                messages = self._maybe_compact(messages)
                continue

            calls = await self._run_calls(response.tool_calls)
            messages.append(
                Message(
                    role="assistant",
                    content=response.content or _render_tool_calls(calls),
                )
            )
            results_text: list[str] = []
            for name, args, result in calls:
                results_text.append(
                    prompts.wrap_tool_result(_source_for(name, args), result.output)
                )
                await repo.insert_event(
                    self.db,
                    "tool_result",
                    {"tool": name, "ok": result.ok, "held": result.held},
                    run_id=self.run_id,
                    attempt_id=self.attempt.id,
                )
            messages.append(Message(role="user", content="\n\n".join(results_text)))
            messages = self._maybe_compact(messages)

            terminal = self._terminal_of(calls)
            if terminal is not None:
                name, args = terminal
                if name == "mark_task_complete":
                    return await self._finish(
                        "succeeded", str(args.get("summary", "")), None, turns_used
                    )
                amendment = (str(args.get("reason", "")), str(args.get("suggested_change", "")))
                return await self._finish(
                    "amendment_requested", None, None, turns_used, amendment=amendment
                )

        return await self._finish(
            "failed", None, "turn budget exhausted (no terminal tool call)", turns_used
        )

    # ------------------------------------------------------------ internals

    async def _run_calls(
        self, tool_calls: list[ModelToolCall]
    ) -> list[tuple[str, dict[str, object], ToolExecResult]]:
        """Defensively parse ``arguments_json``, then execute each call.

        Malformed arguments never raise outward: the model gets a synthetic
        error result and the turn still counts.
        """
        executed: list[tuple[str, dict[str, object], ToolExecResult]] = []
        for tc in tool_calls:
            try:
                args: dict[str, object] = json.loads(tc.arguments_json)
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
            except (ValueError, TypeError) as exc:
                error_output = f"error: malformed tool arguments: {exc}"
                executed.append(
                    (tc.name, {}, ToolExecResult(ok=False, output=error_output))
                )
                continue
            result = await self._registry.execute(tc.name, args)
            executed.append((tc.name, args, result))
        return executed

    def _terminal_of(
        self, calls: list[tuple[str, dict[str, object], ToolExecResult]]
    ) -> tuple[str, dict[str, object]] | None:
        for name, args, _ in calls:
            if name in ("mark_task_complete", "request_spec_amendment"):
                return name, args
        return None

    def _maybe_compact(self, messages: list[Message]) -> list[Message]:
        if not should_compact(messages, self._context_window):
            return messages
        stats = CompactionStats()
        compacted, scratchpad = compact(
            messages, context_window=self._context_window, stats=stats
        )
        return [
            compacted[0],
            Message(role="system", content=f"[TRUSTED] Scratchpad (prior progress):\n{scratchpad}"),
            compacted[1],
            *compacted[2:][-_TAIL_AFTER_COMPACTION:],
        ]

    async def _finish(
        self,
        status: str,
        summary: str | None,
        failure_reason: str | None,
        turns_used: int,
        amendment: tuple[str, str] | None = None,
    ) -> AttemptOutcome:
        await repo.insert_event(
            self.db,
            "attempt_finished",
            {"status": status, "turns_used": turns_used, "reason": failure_reason},
            run_id=self.run_id,
            attempt_id=self.attempt.id,
        )
        return AttemptOutcome(
            status=status,
            summary=summary,
            failure_reason=failure_reason,
            turns_used=turns_used,
            amendment=amendment,
        )
