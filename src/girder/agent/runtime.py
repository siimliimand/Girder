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
import logging
from dataclasses import dataclass
from typing import Any, cast

from girder.agent import prompts
from girder.agent.context import (
    CompactionStats,
    Scratchpad,
    compact,
    scratchpad_message,
    should_compact,
)
from girder.agent.tools import (
    _NEW_TOOL_ALIASES,
    _WRITE_TOOLS,
    TOOL_SCHEMAS,
    ToolExecResult,
    ToolRegistry,
    _gate_new_tool,
)
from girder.config import LimitsConfig
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Attempt, SteeringKind, Task
from girder.guard.redact import Redactor, redact_and_log
from girder.guard.scope import TaskScopes, Verdict, check_tool_call
from girder.models.gateway import Message, ModelGateway, ModelToolCall
from girder.sandbox.engine import SandboxEngine
from girder.stacks import STACK_REGISTRY, StackPlugin

# Recent tail preserved across compaction so the loop stays coherent (§8.5).
_TAIL_AFTER_COMPACTION = 6

# WP 8.1 planning-phase hold: a plain tool RESULT — the tool is not executed,
# the call is not recorded, and it is NOT a held (scope-violated) call, so it
# never taints the attempt's integrity ledger.
_PLANNING_HOLD_OUTPUT = (
    "[planning phase] Write tools are not available until you have output a "
    "PLAN block."
)

_PLAN_MAX_CHARS = 2000

_NO_TOOL_NUDGE = (
    "You must act via tools; call mark_task_complete when the task is done "
    "(or request_spec_amendment if the frozen spec cannot be satisfied)."
)

# Variant for the last allowed turn: remind the agent the budget was announced.
_NO_TOOL_NUDGE_FINAL = _NO_TOOL_NUDGE + (
    " This was your final turn: you were told the turn budget up front — "
    "the attempt now ends without a terminal call."
)


def _milestone_for_turn(turn: int, cap: int) -> str | None:
    """Deterministic budget milestones (Group A): one directive at the half,
    one at cap-2. Each fires on exactly one turn number; None otherwise."""
    if turn == cap - 2:
        return "Final turns: conclude and call `mark_task_complete`."
    if turn == max(1, cap // 2):
        return "Half your turns are spent — start writing now"
    return None


logger = logging.getLogger(__name__)


class _MidTurnDeadline(RuntimeError):
    """Internal: the wall-clock ceiling expired mid-turn (§8.3.2)."""


@dataclass(frozen=True)
class AttemptOutcome:
    status: str  # "succeeded" | "failed" | "timeout" | "amendment_requested" |
    # "integrity_violation" (terminal turn tainted by a held call)
    summary: str | None
    failure_reason: str | None
    turns_used: int
    amendment: tuple[str, str] | None  # (reason, suggested_change)
    # True when the terminal call was mark_task_complete(no_changes=true):
    # the attempt "succeeded" without producing any diff (consumed by
    # task_engine for no-op honesty accounting; set here, never acted on).
    no_changes: bool = False


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
        python_bin: str,
        redactor: Redactor,
        db: Database,
        run_id: str,
        attempt: Attempt,
        task: Task,
        stack: StackPlugin | None = None,
        model_role: str = "tier2",
    ) -> None:
        self.gateway = gateway
        self.limits = limits
        self.db = db
        self.run_id = run_id
        self.attempt = attempt
        self.task = task
        self.model_role = model_role
        self._scopes = scopes  # kept for budget-aware prompts (protected globs)
        self._redactor = redactor
        self._context_window = gateway.role_config(model_role).context_window
        # WP 8.1/8.2 per-attempt state: the structured scratchpad shared with
        # the tool registry, and the planning-phase latch.
        self._scratchpad = Scratchpad()
        self._plan_emitted = False
        self._registry = ToolRegistry(
            sandbox=sandbox,
            container=container,
            scopes=scopes,
            limits=limits,
            python_bin=python_bin,
            redactor=redactor,
            db=db,
            attempt_id=attempt.id,
            run_id=run_id,
            task=task,
            stack=stack or STACK_REGISTRY["python-3.12"],
            scratchpad=self._scratchpad,
        )

    async def execute_attempt(
        self,
        *,
        spec_slice: str,
        guidance: str | None = None,
        deadline_s: float | None = None,
        prior_attempt_summary: str | None = None,
    ) -> AttemptOutcome:
        """Run the turn loop to a terminal tool, the turn cap, or the deadline.

        ``deadline_s`` is an absolute ``asyncio.get_running_loop().time()``
        value; the ceiling is enforced both between turns and *within* a turn
        — the gateway call and each tool execution run under
        ``asyncio.wait_for`` bounded by the remaining wall-clock (§8.3.2).
        Killing the container remains the caller's job.
        ``prior_attempt_summary`` (WP 8.3) lands in the structured scratchpad
        so the retry brief survives compaction.
        """
        self._scratchpad.prior_attempt_summary = prior_attempt_summary
        messages = [
            Message(
                role="system",
                content=prompts.build_system_prompt(
                    task=self.task,
                    spec_slice=spec_slice,
                    turn_budget=self.limits.attempt_max_turns,
                    read_restricted=self._scopes.protected_globs,
                    planning_turns=self.limits.planning_turns,
                ),
            ),
            Message(
                role="user",
                content=prompts.build_task_message(
                    task=self.task,
                    spec_slice=spec_slice,
                    guidance=guidance,
                    read_restricted=self._scopes.protected_globs,
                ),
            ),
        ]
        loop = asyncio.get_running_loop()
        turns_used = 0
        fired_milestones: set[str] = set()
        while turns_used < self.limits.attempt_max_turns:
            messages = await self._absorb_steering(messages)
            remaining = self._remaining_s(deadline_s, loop)
            if remaining is not None and remaining <= 0:
                return await self._finish(
                    "timeout", None, "wall-clock deadline exhausted", turns_used
                )
            turns_used += 1
            # Budget milestones ride the same trusted-directive framing as
            # operator steering (build_directive_message); each fires once.
            milestone = _milestone_for_turn(turns_used, self.limits.attempt_max_turns)
            if milestone is not None and milestone not in fired_milestones:
                fired_milestones.add(milestone)
                self._scratchpad.milestones.append(milestone)
                messages.append(
                    Message(role="user", content=prompts.build_directive_message(milestone))
                )
            await repo.insert_event(
                self.db,
                "turn_start",
                {"turn": turns_used, "messages": len(messages)},
                run_id=self.run_id,
                attempt_id=self.attempt.id,
            )

            # Persist the exact outgoing prompt BEFORE the gateway call
            # (state-before-action, §5.5); redacted per §8.6 since prompts
            # embed tool output. Observability only: fail open.
            await self._persist_prompt(turns_used, messages)

            try:
                response = await asyncio.wait_for(
                    self.gateway.complete(
                        self.model_role,
                        messages,
                        tools=TOOL_SCHEMAS,
                        run_id=self.run_id,
                        attempt_id=self.attempt.id,
                    ),
                    timeout=remaining,
                )
            except TimeoutError:
                return await self._finish(
                    "timeout", None, "wall-clock deadline exhausted mid-turn", turns_used
                )

            # WP 8.1: a PLAN block in the response unlocks write tools from
            # this turn on (the planning budget is a ceiling, not a delay).
            # Checked BEFORE the no-tool-call branch (B1): a compliant model
            # emits the plan as plain text with no tool calls, and that turn
            # must still arm the latch and store the plan.
            if not self._plan_emitted and "PLAN:" in (response.content or ""):
                self._plan_emitted = True
                self._scratchpad.plan = (response.content or "")[:_PLAN_MAX_CHARS]

            if not response.tool_calls:
                # No action: nudge and count the turn; cap handled by the loop.
                # Final-turn nudge reminds the agent the budget was announced.
                nudge = (
                    _NO_TOOL_NUDGE_FINAL
                    if turns_used >= self.limits.attempt_max_turns
                    else _NO_TOOL_NUDGE
                )
                messages.append(Message(role="assistant", content=response.content or ""))
                messages.append(Message(role="user", content=nudge))
                messages = self._maybe_compact(messages)
                continue

            try:
                calls = await self._run_calls(
                    response.tool_calls, deadline_s=deadline_s, turn=turns_used
                )
            except _MidTurnDeadline:
                return await self._finish(
                    "timeout", None, "wall-clock deadline exhausted mid-turn", turns_used
                )
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
                # §8.5/D11: a terminal tool declared in the SAME turn as a
                # held (violated) call is not a clean completion — the held
                # call never executed, so "done" rests on work the
                # orchestrator refused to run. Fail without retry; the
                # integrity ledger row was already written by the registry.
                # A held call in an EARLIER turn followed by a clean terminal
                # turn stays as-is (surfaced via the ledger + delivery gate).
                if any(result.held for _, _, result in calls):
                    return await self._finish(
                        "integrity_violation",
                        None,
                        "terminal tool called in the same turn as a held call",
                        turns_used,
                    )
                if name == "mark_task_complete":
                    # Terminal gate: a dirty worktree at "complete" is exactly
                    # how verify kills the attempt (uncommitted-leftovers
                    # audit) — bounce instead, the model can still commit
                    # within this attempt. Probe failure fails open; verify
                    # remains the backstop. (Dogfood fdee1ec1: 3/3 attempts
                    # lost to a skipped commit step.)
                    dirty, status_out = await self._registry.worktree_dirty_with_output()
                    if dirty:
                        await repo.insert_event(
                            self.db,
                            "terminal_bounced",
                            {"tool": name, "status_out": status_out[-500:]},
                            run_id=self.run_id,
                            attempt_id=self.attempt.id,
                        )
                        messages.append(
                            Message(
                                role="user",
                                content=(
                                    "ATTEMPT NOT ACCEPTED — the worktree has uncommitted"
                                    " changes. Run `git add -A && git commit -m"
                                    " \"<short message>\"` to commit your work, then call"
                                    " mark_task_complete again. (no_changes=true is only"
                                    " valid on a clean tree.)"
                                ),
                            )
                        )
                        messages = self._maybe_compact(messages)
                        continue
                    return await self._finish(
                        "succeeded",
                        str(args.get("summary", "")),
                        None,
                        turns_used,
                        no_changes=bool(args.get("no_changes", False)),
                    )
                amendment = (str(args.get("reason", "")), str(args.get("suggested_change", "")))
                return await self._finish(
                    "amendment_requested", None, None, turns_used, amendment=amendment
                )

        return await self._finish(
            "failed",
            None,
            "turn budget exhausted (no terminal tool call) — you were told "
            "the budget up front: explore less, write sooner",
            turns_used,
        )

    # ------------------------------------------------------------ internals

    async def _absorb_steering(self, messages: list[Message]) -> list[Message]:
        """Mid-flight operator steering (Sprint 6 WP 6.2): consume pending
        ``inject`` events at the top of each turn and append them as trusted
        user messages (tagged by :func:`prompts.build_directive_message`).
        Blank/malformed directives are skipped and logged, never raise."""
        events = await repo.consume_steering_events(
            self.db, self.run_id, kinds=[SteeringKind.INJECT.value]
        )
        for event in events:
            payload = event["payload"]
            directive = ""
            if isinstance(payload, dict):
                directive = str(payload.get("directive", "") or "").strip()
            if not directive:
                await repo.insert_event(
                    self.db,
                    "steering_ignored",
                    {"kind": SteeringKind.INJECT.value, "reason": "blank directive"},
                    run_id=self.run_id,
                    attempt_id=self.attempt.id,
                )
                continue
            messages.append(
                Message(role="user", content=prompts.build_directive_message(directive))
            )
            await repo.insert_event(
                self.db,
                "steering_injected",
                {"directive": directive},
                run_id=self.run_id,
                attempt_id=self.attempt.id,
            )
        return messages

    async def _run_calls(
        self, tool_calls: list[ModelToolCall], *, deadline_s: float | None = None, turn: int = 0
    ) -> list[tuple[str, dict[str, object], ToolExecResult]]:
        """Defensively parse ``arguments_json``, then execute each call.

        Malformed arguments never raise outward: the model gets a synthetic
        error result and the turn still counts. Each execution is bounded by
        the remaining wall-clock (§8.3.2); exhaustion raises
        :class:`_MidTurnDeadline` so the caller takes the timeout path.
        WP 8.1 (R-SP8-1): during the planning phase a write tool returns a
        plain synthetic result — the registry never sees the call, so no
        tool_calls row (held or otherwise) is written and the attempt's
        integrity ledger stays clean.
        """
        loop = asyncio.get_running_loop()
        planning = not self._plan_emitted and turn <= max(0, self.limits.planning_turns)
        executed: list[tuple[str, dict[str, object], ToolExecResult]] = []
        for tc in tool_calls:
            if planning and tc.name in _WRITE_TOOLS:
                # The planning hold never launders a would-be integrity
                # violation: a write the scope gate would hold still goes
                # through the registry and is recorded as held (R-SP8-1).
                try:
                    hold_args = cast("dict[str, Any]", json.loads(tc.arguments_json))
                    verdict = check_tool_call(tc.name, hold_args, self._scopes)
                    if verdict is Verdict.ALLOW and tc.name in _NEW_TOOL_ALIASES:
                        verdict = _gate_new_tool(tc.name, hold_args, self._scopes)
                except (ValueError, TypeError):
                    verdict = Verdict.ALLOW
                if verdict is not Verdict.VIOLATION:
                    executed.append(
                        (tc.name, {}, ToolExecResult(ok=False, output=_PLANNING_HOLD_OUTPUT))
                    )
                    # R1/R3 instrumentation: countable planning-phase holds
                    # for dogfood analysis (same fire-and-forget machinery as
                    # tool_result / terminal_bounced).
                    await repo.insert_event(
                        self.db,
                        "planning_hold",
                        {"tool": tc.name, "turn": turn},
                        run_id=self.run_id,
                        attempt_id=self.attempt.id,
                    )
                    continue
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
            remaining = self._remaining_s(deadline_s, loop)
            if remaining is not None and remaining <= 0:
                raise _MidTurnDeadline
            try:
                result = await asyncio.wait_for(
                    self._registry.execute(tc.name, args), timeout=remaining
                )
            except TimeoutError:
                raise _MidTurnDeadline from None
            executed.append((tc.name, args, result))
        return executed

    @staticmethod
    def _remaining_s(deadline_s: float | None, loop: asyncio.AbstractEventLoop) -> float | None:
        """Seconds of wall-clock left under the attempt ceiling (§8.3.2)."""
        if deadline_s is None:
            return None
        return deadline_s - loop.time()

    async def _persist_prompt(self, turn: int, messages: list[Message]) -> None:
        """Persist the exact outgoing prompt snapshot (plan.md Phase 5 task 5).

        The serialized message list is redacted through the same §8.6 pipeline
        tool outputs use (``redact_and_log``, writing ``redaction_log`` rows)
        before storage, because prompts embed raw tool output. Purely
        observability: any failure logs and continues, never killing the turn.
        """
        payload = json.dumps(
            [{"role": m.role, "content": m.content} for m in messages], default=str
        )
        try:
            redacted = await redact_and_log(
                self._redactor,
                payload,
                source_field="prompt:turn",
                db=self.db,
                attempt_id=self.attempt.id,
            )
            await repo.record_attempt_prompt(
                self.db,
                self.attempt.id,
                turn=turn,
                role="turn",
                content=redacted,
                run_id=self.run_id,
            )
        except Exception:
            logger.warning(
                "prompt persistence failed (attempt %s, turn %s); continuing",
                self.attempt.id,
                turn,
                exc_info=True,
            )

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
        # WP 8.2: with a structured scratchpad the refresh is free — the JSON
        # block at index 2 is regenerated from registry-maintained state and
        # NO distillation pass runs over the conversation (no model call).
        # compact() falls back to free-text distillation only for callers
        # without a structured scratchpad.
        compacted, _scratchpad = compact(
            messages, context_window=self._context_window, stats=stats, structured=self._scratchpad
        )
        # Invariant: messages[:2] is always [system, task brief] — the new
        # scratchpad goes after the head pair, so repeated compactions never
        # displace the task brief (which carries the frozen spec slice).
        return [
            compacted[0],
            compacted[1],
            scratchpad_message(self._scratchpad),
            *compacted[2:][-_TAIL_AFTER_COMPACTION:],
        ]

    async def _finish(
        self,
        status: str,
        summary: str | None,
        failure_reason: str | None,
        turns_used: int,
        amendment: tuple[str, str] | None = None,
        no_changes: bool = False,
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
            no_changes=no_changes,
        )
