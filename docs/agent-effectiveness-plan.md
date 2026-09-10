# Agent-effectiveness fix pass — budget awareness, work salvage, guard fix, wave abort

> Plan of 2026-09-10, derived from the first two dogfood runs (1c176931, 4917da28) reaching the wave
> agent loop. Root-cause findings were verified by live monitoring and code replay; see git history
> for the incident commits (a17c33b sandbox fix, 9d537eb steer gate).

## Context

The first two dogfood runs lost 7 attempts, every one to "turn budget exhausted (no terminal tool
call)" after pure read-only exploration with zero diffs, plus 2 integrity kills (one caused by a
guard false positive). Deep exploration found the root causes are **structural, not model
stupidity**:

1. The agent is **never told it has a turn budget** — not in the system prompt (which is 100%
   prohibition-invariants, `agent/prompts.py:13-32`), not in tool results, not at death. It cannot
   budget.
2. **Dead attempts lose all their work**: on turn-cap death the worktree is force-pruned
   (`task_engine._teardown_attempt`), uncommitted work is destroyed, no `attempt_diffs` row exists —
   attempt N+1 re-explores from scratch and dies identically (observed 3/3 and 2/2).
3. `read_file` truncates at **100 lines** (`tool_output_max_lines=100`), so a 600-line file takes 6
   calls; nothing teaches the outline→range-read funnel; descriptions are one-liners with no
   strategy.
4. Retry guidance for a turn-cap death is literally
   `"Previous attempt failed: turn budget exhausted (no terminal tool call)"`
   (`task_engine.py:353`) — no advice, no mention of lost work.
5. A no-op (empty diff) is accepted for **every** task type (`task_engine.py:559-563`) — a docs
   task reached `verifying` having written nothing.
6. Guard false positive: sed/awk address scripts (`sed -n '/re/,/re/p' f`) are lexically classified
   as absolute-path escapes → held → integrity kill (reproduced via `_check_run_command`).
7. Wave abort watcher is cancelled when the **first** wave task settles
   (`run_engine.py:476-501`) — the rest of the wave runs unprotected (observed live: abort sat
   unconsumed 3 min, operator had to kill the daemon).
8. `turns_used` is never persisted to the attempts table (no
   `update_attempt_fields(turns_used=...)` anywhere), hiding the tuning signal.

## Changes

### A. Budget awareness + working method in prompt space — `src/girder/agent/prompts.py`, `src/girder/agent/runtime.py`

- **System prompt** (`build_system_prompt`): add a compact "working method" block: the turn cap is
  N (format from settings); survey before reading (`find_files`/`view_symbol_outline`), read ranges
  not whole files, **start writing by half budget**; `mark_task_complete` MUST end the attempt —
  running out of turns kills the attempt and discards uncommitted work. Frame protected paths as
  read-forbidden.
- **Milestone directives** (runtime loop): deterministic `[TRUSTED]` user-message injections,
  reusing the existing directive-message path (`build_directive_message`): at 50% budget — "half
  your turns are spent; start writing now"; at `N-2` — "final turns: conclude and call
  `mark_task_complete`". No model cooperation required.
- **Task brief**: include read-restricted globs (`Write scope` already present;
  add `Read-restricted: {protected_globs}` so protected-path holds are never a surprise).

### B. Work salvage across ordinary retries — `src/girder/orchestrator/task_engine.py` (core-logic change)

For ordinary-retryable failures (**turn-cap, timeout, verify-fail, uncommitted-leftovers** — not
integrity, not amendment): before teardown, if the attempt worktree is dirty, the **orchestrator
mechanically commits** the leftovers to the task branch:
`git add -A && git commit -m "wip(attempt N): salvaged on retry"` (redaction not needed — files are
already in-scope writes). Attempt N+1 already bases from the task branch tip, so salvaged work is
present; guidance for the retry states: *"Attempt N ran out of turns; its work was salvaged as
commit \<sha\> (files: …). Continue from there — do not redo completed work; finish and call
mark_task_complete."* The uncommitted-leftovers verify check then stays meaningful for attempt
N+1's *own* new leftovers.

- Confirm exact branch/base mechanics against `_start_attempt`/WorktreeManager during
  implementation; keep `attempt_diffs` capture unchanged.
- This is the highest-impact change: it converts a 3×-identical failure into incremental progress.

### C. Tool surface honesty — `src/girder/agent/tools.py`, `girder.toml`

- `read_file` description: state the returned line budget and preach the funnel ("for large files
  call `view_symbol_outline` first, then read ranges via `line_start`/`line_end`").
- `mark_task_complete`: description rewritten — "the ONLY way to finish; the attempt is destroyed
  if turns run out" — and gains `no_changes: bool = false` for explicit no-op declarations.
- `girder.toml [limits]`: `tool_output_max_lines 100→400`, `tool_output_max_tokens 4000→16000`,
  `attempt_max_turns 40→60` (fewer, fatter turns beat many clipped ones on a 1.31M-context model;
  config keys already exist).

### D. No-op honesty — `src/girder/orchestrator/task_engine.py`

At the no-op acceptance point (L559-563): empty diff is accepted **only** when the agent declared
`no_changes=true` on `mark_task_complete`; otherwise it becomes an ordinary retryable failure with
guidance ("you declared the task complete but no changes exist; if genuinely nothing is needed,
call `mark_task_complete(no_changes=true)`; otherwise write the change"). Update the sc05 e2e
("no change needed" agent) to declare `no_changes=true`.

### E. Guard false positive — `src/girder/guard/scope.py`

In `_looks_like_path` (read-shaped `others` bucket only — the write path uses positional
redirect/tee detection and is untouched): reject tokens that (a) contain path-implausible
characters (`` ` ``, `^`, `[`, `]`, `$`, `(`, `)`, `|`, `;`), or (b) match sed-script shape
`` /…/letters `` (regex address with trailing script letters, e.g. `/TODO/d`, `/x/,/y/p`).
Absolute paths with no such markers (`/etc/passwd`) still classify as escapes → VIOLATION; the
multi-token smuggling defense (`cat a.py .github/x`) is unaffected. Regression tests in
`tests/unit/test_scope.py` idiom (`is Verdict.ALLOW_LOGGED`), including the exact command from
today's kill, plus negative tests (redirect to `/etc/x` stays VIOLATION,
`cat a.py .github/y` stays VIOLATION).

### F. Wave abort re-arm — `src/girder/orchestrator/run_engine.py`

`_run_attempt_with_abort_watch`: replace the single `FIRST_COMPLETED` wait with a loop —
`asyncio.wait` over `{watcher, *pending}`; if the watcher fired → `_abort_run` + return None; else
drop settled tasks and re-wait until `pending` is empty; cancel the watcher only after all tasks
are done. Unit test with two fake coroutines (fast settling + long-running with side effect) and
an abort inserted after the first settles → long one must be cancelled and CRASHED (no full e2e
fixture stack needed; e2e `test_steering_abort.py` pattern noted as the model).

### G. Turn telemetry — `src/girder/orchestrator/task_engine.py`

Persist `turns_used` on every close path: pass `outcome.turns_used` into
`_close_attempt`/`update_attempt_fields` at the retry/fail sites (L305-310, L345-353, verify-fail
path). One assertion added to an existing task_engine test.

### H. Task sizing at the source — `src/girder/specs/generator.py` (prompt clause only)

Add one clause to the spec-generator prompt: tasks must be scoped so one implementer can finish
within the turn budget — split document-sized work (e.g. "a complete configuration reference")
into per-section tasks. No validator/decomposer schema changes.

## Deliberately not in this pass

- Mid-attempt console steering of running attempts (inject already works live; parked-run gate
  shipped in 9d537eb)
- Compaction/scratchpad changes (never triggers at these context sizes)
- Baseline traceback persistence (separate observability fix)

## Verification

1. `uv run pytest tests/unit -q` (678+ green) + `uv run pytest -q` full; `ruff check` +
   `mypy --strict` on touched files (repo standard).
2. Guard: new regression tests red on old code, green on new (including the literal sed command
   from the incident).
3. Abort: new unit test red on old code (long coroutine runs to completion), green on new
   (cancelled + CRASHED).
4. Salvage: task_engine unit test — turn-cap death with dirty worktree → WIP commit on task
   branch, attempt 2 brief contains salvage guidance, work present (FakeGateway pattern from
   `test_task_engine.py`).
5. No-op: sc05 e2e updated and green; new test that undeclared no-op ⇒ retry with guidance.
6. Commit as one fix pass (files: agent/prompts.py, agent/runtime.py, agent/tools.py,
   orchestrator/task_engine.py, orchestrator/run_engine.py, guard/scope.py, specs/generator.py,
   girder.toml, tests).
7. Live validation: restart daemon, fresh dogfood run — success signal: attempts show
   `turns_used > 0`, non-empty `attempt_diffs` from attempt 1, no turn-cap deaths without salvage,
   abort lands mid-wave if pressed.
