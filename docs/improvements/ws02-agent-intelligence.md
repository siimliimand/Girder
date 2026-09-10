# WS-02 — Agent Intelligence Layer

| | |
|---|---|
| **Source spec** | `docs/improvements-plan.md` v1.0 · Sprint 8 (WP 8.1 – WP 8.3) |
| **Status** | Merged to main (c941db4) + adversarial-review fixes (ec1f6b3) — pending dogfood validation of success rate / turns-per-success (operator task) |
| **Wave** | **2** — starts after **WS-01 merges** (needs the WS-01 tool surface for planning-phase tool lists and scratchpad hooks) |
| **Effort** | ~2 weeks · ~30 new tests |
| **Owned files** | `src/girder/agent/prompts.py`, `src/girder/agent/runtime.py`, `src/girder/agent/context.py`, `src/girder/orchestrator/task_engine.py`, `src/girder/config.py` |
| **Shared files** | `task_engine.py` — WS-03 adds a one-line index hook (wave separation avoids conflict); `config.py` `LimitsConfig` — additive keys only |

---

## Goal

Improve agent reasoning quality without changing the model: a mandatory planning phase, a structured scratchpad, and retry briefs that carry real information from the failed attempt.

## Current state (verified 2026-09-10)

- `AgentRuntime.execute_attempt()` already accepts a `guidance: str | None` parameter (`runtime.py:146`) and injects trusted guidance via `prompts.build_directive_message` — WP 8.3 plugs into this existing path.
- **Work salvage (Group B) already shipped**: `task_engine.py` tracks salvage commits per failed attempt (`_salvage_tips`) and starts retry worktrees at the salvaged task-branch tip. WP 8.3 *surfaces* this in the retry brief; do not re-implement salvage.
- `LimitsConfig` exists at `config.py:89` — add the new keys there.
- No planning phase, no structured scratchpad, no retry brief exist yet.

## Definition of Done

- Dogfood **success rate** (attempts reaching `mark_task_complete` on first try) improves from baseline; **turns-used-per-success decreases**. Measured by re-running the dogfood scenarios with the new prompts.
- Write tools are mechanically held during the planning phase (not prompt-only).
- Compaction no longer burns a distillation model call when the structured scratchpad is available.
- All new unit tests green; `mypy --strict` clean; e2e SC-01 passes.

**Pre-flight:** re-run one dogfood scenario to record the current baseline success rate and turns-per-success before changing prompts.

---

## WP 8.1 — Mandatory Planning Phase

**Why:** Dogfood data shows agents spending the first 10–15 turns exploring, then dying at the turn cap. Plan-before-execute discipline eliminates this.

**Implementation:**

Add a `planning_turns` budget (default 5) at the start of every attempt where the agent is expected to output a structured plan but not modify any files. Enforced at three layers:

1. **Prompt addition in `build_system_prompt`** (`src/girder/agent/prompts.py`):

```
REQUIRED PLANNING PHASE (turns 1–5):
Before writing ANY file, output a plan in this format:
  PLAN:
  - Read: [list files you need to read]
  - Understand: [what you need to learn from them]
  - Write: [list files to create or modify, one per bullet]
  - Test: [which tests you'll run to verify]

Do NOT call write_file, edit_file, or apply_patch before outputting PLAN.
After outputting PLAN, proceed to execution.
```

2. **Runtime enforcement** (`src/girder/agent/runtime.py`): track whether the agent has output a plan. For the first `planning_turns` turns, any write tool call returns a synthetic held result: `"[planning phase] Write tools are not available until you have output a PLAN block."` After a PLAN block is detected in `response.content`, write tools unlock. The plan text is extracted and stored as the initial scratchpad.

3. **Config key** `limits.planning_turns: int = 5` in `LimitsConfig` (`src/girder/config.py`); surfaces in `girder.toml` as `[limits] planning_turns = 5`.

**Tests** (`tests/unit/test_agent_runtime.py`): write tools held during planning phase; unlock after PLAN block; compaction preserves the plan.

---

## WP 8.2 — Structured Scratchpad

**Why:** The current scratchpad is free-text distilled by the model during compaction. It loses structure and the compaction call consumes tokens. A structured scratchpad is maintained by the orchestrator — not the model — and survives compaction with zero token cost.

**Implementation** — `Scratchpad` dataclass in `src/girder/agent/context.py`:

```python
@dataclass
class Scratchpad:
    plan: str                            # From planning phase (WP 8.1)
    files_read: list[str]                # Accumulated by tool registry
    files_written: list[str]             # Accumulated by tool registry
    test_results: list[str]              # From run_tests tool (WS-01)
    milestones: list[str]                # Injected by runtime at key turns
    prior_attempt_summary: str | None    # From retry brief (WP 8.3)
```

- The `ToolRegistry` populates `files_read` / `files_written` / `test_results` on every tool execution. (This is the WS-01 dependency: the registry must map tool name → structured result.)
- Serialized as a compact JSON block, injected as a `[TRUSTED] Scratchpad:` system message at the top of the context after the task brief.
- On compaction, only the scratchpad message is refreshed — **the distillation model call is eliminated**.

**Migration:** keep the existing free-text compaction in `compact()` as a fallback, firing only when the structured scratchpad is unavailable (pre-8.2 data).

---

## WP 8.3 — Richer Retry Briefs

**Why:** Current retry guidance (`"Previous attempt failed: turn budget exhausted"`) provides zero actionable information. The orchestrator has rich data from the failed attempt — it should surface it.

**Implementation in `src/girder/orchestrator/task_engine.py`** — extract a `_build_retry_brief()` function:

```python
def _build_retry_brief(
    prev_attempt: Attempt,
    prev_diff: str | None,
    prev_test_results: JUnitResult | None,
    prev_violations: list[IntegrityViolation],
    turns_used: int,
) -> str:
    lines = [f"[TRUSTED] Retry brief (attempt {prev_attempt.attempt_num} failed):"]
    lines.append(f"- Failure reason: {prev_attempt.failure_reason or 'turn budget exhausted'}")
    lines.append(f"- Turns used: {turns_used} of {budget}")

    if prev_diff:
        changed = [l[2:] for l in prev_diff.splitlines() if l.startswith('+++ ')]
        lines.append(f"- Files with changes: {', '.join(changed) or 'none'}")
        lines.append("- Salvaged work is committed to the task branch — do not redo it.")
    else:
        lines.append("- No changes were saved from the previous attempt.")

    if prev_test_results and prev_test_results.failures:
        lines.append("- Test failures from previous attempt:")
        for f in prev_test_results.failures[:5]:
            lines.append(f"    {f.classname}::{f.name}: {f.message[:200]}")

    if prev_violations:
        lines.append("- Scope violations (tool calls that were blocked):")
        for v in prev_violations[:3]:
            lines.append(f"    {v.kind}: {v.detail.get('tool')} on {v.detail.get('path','?')}")

    lines.append("")
    lines.append("Continue from the salvaged state. Do not re-explore already-read files.")
    return "\n".join(lines)
```

The brief is passed as the `guidance` parameter to `AgentRuntime.execute_attempt()` (path already exists — see Current state), which injects it via `build_directive_message` in the initial task message. Feed `prior_attempt_summary` into the WP 8.2 scratchpad.

**Tests** (`tests/unit/test_task_engine.py`): retry brief constructed for turn-cap death with dirty worktree; brief contains the salvage commit reference.

---

## Coordination

- **WS-01** must merge first: planning-phase tool lists reference `edit_file` / `run_tests`, and the scratchpad hooks the tool registry that WS-01 reshapes.
- **WS-03** adds a one-line index-build hook in `task_engine.py` (attempt start). Land in separate waves; if rebasing, the hook and `_build_retry_brief` are in different regions of the file.
- **WS-06** will later split `task_engine.py` (WP 10.2) — `_build_retry_brief` belongs in the retry-loop module after that split.
- **2026-09-10 review pass (post-merge):** adversarial review of the landing found and fixed in `ec1f6b3` — (B1) a plan-only turn (PLAN text, no tool calls — the maximally compliant behavior) never armed the unlock latch; detection hoisted above the no-tool-call branch so the latch arms and `scratchpad.plan` is stored. (B2) the verify-fail retry path still built the old one-liner; both retry paths now build the structured brief, salvage wording is single-sourced, and `VerificationResult.failing_tests` threads through `retry_step`. (B3) model-authored plan text was re-injected under the `[TRUSTED]` scratchpad; the plan key is now labeled "untrusted model output, verbatim". Planning holds emit `planning_hold` events for dogfood instrumentation. Flagged, NOT fixed: `conflict.py` resolver inherits `planning_turns = 5` without PLAN instructions (up to 5 held turns per resolution attempt); `run_verification`'s suite-green logic ignores per-test statuses in the XML (exit 0 + red XML counts green); `run_command` remains a write-vector during planning (per spec, R-SP8-1 holds write tools only).

## Relevant resolution log decision

- **R-SP8-1:** Planning phase enforced by the orchestrator (held tool calls), not by prompt alone — prompt-only enforcement is a speed bump; orchestrator enforcement is mechanical.
