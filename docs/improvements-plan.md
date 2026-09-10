# Girder — Improvements Implementation Plan

**Version:** 1.0 · **Author:** Code analysis 2026-09-10 · **Status:** Proposed  
**Prerequisite:** Sprints 1–6 complete, 546 tests green, dogfood runs 1–2 completed  
**Companion docs:** `docs/plan.md` v2.2 (spec), `docs/implementation-plan.md` (engineering blueprint), `docs/agent-effectiveness-plan.md` (Group A–H fix pass)

This document describes the next phase of Girder development. It covers every identified gap between the current implementation and a production-grade, Jules/Devin-class autonomous delivery system. Improvements are grouped into seven sprints ordered by ROI: agent effectiveness first, infrastructure last.

---

## Table of Contents

1. [Sprint 7 — Agent Tooling Expansion](#sprint-7--agent-tooling-expansion)
2. [Sprint 8 — Agent Intelligence Layer](#sprint-8--agent-intelligence-layer)
3. [Sprint 9 — GitHub Automation Pipeline](#sprint-9--github-automation-pipeline)
4. [Sprint 10 — Architecture Refactor](#sprint-10--architecture-refactor)
5. [Sprint 11 — Multi-Stack Support](#sprint-11--multi-stack-support)
6. [Sprint 12 — Web Console & Observability](#sprint-12--web-console--observability)
7. [Sprint 13 — Self-Hosting & Productionisation](#sprint-13--self-hosting--productionisation)
8. [Resolution Log](#resolution-log)

---

## Sprint 7 — Agent Tooling Expansion

**Goal:** Give agents a richer, more precise tool surface. This is the highest single-ROI change: better tools eliminate the exploration spirals documented in the dogfood runs without requiring any model improvement.

**Definition of Done:** All new tools pass scope gating, redaction, and tool-call logging through the existing pipeline; `mypy --strict` clean; all new unit tests green; e2e SC-01 still passes with the expanded tool surface.

---

### WP 7.1 — `edit_file` Tool (line-range replacement)

**Why:** `write_file` replaces the entire file. On a 600-line file an agent must re-transmit all 600 lines to change 5 of them, wasting tokens and introducing transcription errors. A line-targeted edit tool is the single biggest agent effectiveness gain possible with no model changes.

**Implementation — `src/girder/agent/tools.py`:**

Add a new tool schema:

```python
{
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": (
            "Replace a contiguous block of lines in a file. "
            "Use view_symbol_outline + read_file first to identify exact line numbers. "
            "start_line and end_line are 1-indexed and inclusive. "
            "The replacement text replaces those lines exactly — "
            "do not include surrounding context lines."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path":         {"type": "string"},
                "start_line":   {"type": "integer", "description": "First line to replace (1-indexed, inclusive)"},
                "end_line":     {"type": "integer", "description": "Last line to replace (1-indexed, inclusive)"},
                "replacement":  {"type": "string",  "description": "New content for lines start_line..end_line"},
            },
            "required": ["path", "start_line", "end_line", "replacement"],
        },
    },
}
```

Sandbox execution via a Python one-liner (same `_B64_DECODE_SNIPPET` pattern, no stdin):

```python
_EDIT_SNIPPET = (
    "import sys, pathlib; "
    "p = pathlib.Path(sys.argv[1]); "
    "lines = p.read_text().splitlines(keepends=True); "
    "s, e = int(sys.argv[2]) - 1, int(sys.argv[3]); "
    "import base64; repl = base64.b64decode(sys.argv[4]).decode(); "
    "lines[s:e] = [repl] if repl.endswith('\\n') else [repl + '\\n']; "
    "p.write_text(''.join(lines))"
)
```

Scope gate applies the same write-policy as `write_file` — the `path` argument determines the verdict.

**Tests:** Unit test in `tests/unit/test_agent_tools.py` — edit middle, start, and end of a file; verify character-level accuracy; verify scope violations are held.

---

### WP 7.2 — `list_directory` Tool

**Why:** Agents currently enumerate files by shelling out to `ls` or using `find_files` with a wildcard glob. A dedicated structured tool saves turns and produces token-efficient output.

**Schema:**

```python
{
    "type": "function",
    "function": {
        "name": "list_directory",
        "description": (
            "List entries in a directory. Returns name, type (file/dir), "
            "and size for each entry. Depth controls recursion (1 = direct children only). "
            "Use this instead of 'ls' or 'find' when surveying a new area."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path":  {"type": "string", "default": "."},
                "depth": {"type": "integer", "default": 1, "description": "Max recursion depth (1–3)"},
            },
            "required": [],
        },
    },
}
```

Execution via a compact Python one-liner using `os.walk` with depth limit.  
Output format: one line per entry, `[dir]` or `[file N bytes]` prefix. Truncated to `_MATCH_CAP` lines.

**Scope gate:** Read-only — applies the `ALLOW_LOGGED` verdict for in-scope reads.

---

### WP 7.3 — `git_status` and `git_diff` Tools

**Why:** Agents call `run_command` with `git status` / `git diff` multiple times per attempt. Dedicated tools produce consistently structured output and bypass the `run_command` denylist screening overhead.

**`git_status` schema:** No parameters. Returns `git status --short` output.

**`git_diff` schema:**
```python
{
    "parameters": {
        "type": "object",
        "properties": {
            "path":  {"type": "string", "description": "Restrict diff to this file (optional)"},
            "staged":{"type": "boolean", "default": False},
        },
        "required": [],
    },
}
```

Both tools are read-only; scope gate applies `ALLOW_LOGGED` (they read the git object store, which is always in-scope).

---

### WP 7.4 — `run_tests` Tool

**Why:** Agents run pytest via `run_command`. The raw output is multi-hundred lines. A dedicated tool can: (a) run only relevant test files, (b) return structured pass/fail/error counts and per-test summaries, (c) clip output intelligently to the highest-signal parts (failure tracebacks, not passing dots).

**Schema:**
```python
{
    "type": "function",
    "function": {
        "name": "run_tests",
        "description": (
            "Run the project test suite (or a subset) and return a structured summary. "
            "Prefer this over 'run_command' with pytest — it returns structured results "
            "and clips verbose passing output automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Test files or directories to run (empty = full suite)",
                },
                "keyword": {"type": "string", "description": "pytest -k filter expression"},
                "timeout_s": {"type": "number", "default": 120},
            },
            "required": [],
        },
    },
}
```

Execution: runs `pytest --tb=short --no-header -q [paths] [-k keyword] --junitxml=/tmp/.girder-run-tests.xml` inside the sandbox container, then parses the JUnit XML with the existing `parse_junit_xml` from `orchestrator/baseline.py` to produce:

```
PASSED: 42  FAILED: 2  ERROR: 0

FAILURES:
  test_api.py::test_rate_limit — AssertionError: expected 429, got 200
    File "src/api.py", line 47, in handle_request
      if self.limiter.check(key):
  test_api.py::test_headers — KeyError: 'X-Rate-Limit-Remaining'
```

The failure section is always shown in full; the passing summary is one line. This is 10× more token-efficient than raw pytest output.

**Scope gate:** `run_tests` is treated the same as `run_command` (write-policy on path args).

---

### WP 7.5 — `search_symbols` Tool (semantic code search)

**Why:** `ripgrep` is text search. Agents need to find *definitions* ("where is class RateLimiter defined?"), *usages* ("what calls handle_request?"), and *imports* ("what does module X export?"). Currently this takes 3–5 ripgrep calls.

**Schema:**
```python
{
    "type": "function",
    "function": {
        "name": "search_symbols",
        "description": (
            "Find symbol definitions or usages across the codebase. "
            "kind='definition' finds where a symbol is defined; "
            "kind='usage' finds all call sites; "
            "kind='export' lists what a module exports."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name (class, function, variable)"},
                "kind": {
                    "type": "string",
                    "enum": ["definition", "usage", "export"],
                    "default": "definition",
                },
                "path": {"type": "string", "description": "Restrict search to this path"},
            },
            "required": ["name"],
        },
    },
}
```

**Implementation strategy (two phases):**
1. **Phase 1 (MVP):** Implemented as intelligent ripgrep composition. `definition` → `rg "^(def|class|async def)\s+{name}\b"`. `usage` → `rg "\b{name}\s*\("`. `export` → `rg "__all__"` + parse. Ships in Sprint 7.
2. **Phase 2 (Sprint 11):** Replace with `python3 -c` using `ast.parse` for Python, `tree-sitter` for other languages. Ships with multi-stack support.

---

### WP 7.6 — Tool Description Audit

All existing tool descriptions in `TOOL_SCHEMAS` should be reviewed against actual dogfood behavior. Specific changes:

| Tool | Current issue | Fix |
|---|---|---|
| `read_file` | Already improved in Group C of effectiveness plan | Verify shipped |
| `view_symbol_outline` | Says "Python" only but uses grep fallback for others — misleading | State language support clearly |
| `find_files` | Says "fd if available" but agent doesn't know if fd is available | Remove the implementation detail; say "find files matching glob" |
| `ripgrep` | Description says "rg if available" — same issue | Say "regex search across the worktree" |
| `run_command` | Denylist is not surfaced in the description | Add: "Denied commands: curl, wget, nc, ssh, git push, sudo, podman, docker, mount" |

---

## Sprint 8 — Agent Intelligence Layer

**Goal:** Improve agent reasoning quality without changing the model. Add a planning phase, structured scratchpad, richer context injection, and smarter retry briefs.

**Definition of Done:** Dogfood success rate (attempts reaching `mark_task_complete` on first try) improves from baseline; turns-used-per-success decreases. Measured by re-running the dogfood scenarios with the new prompts.

---

### WP 8.1 — Mandatory Planning Phase

**Why:** The dogfood data shows agents spending the first 10–15 turns exploring, then dying at the turn cap. A mandatory plan-before-execute discipline eliminates this.

**Implementation:**

Add a `planning_turns` budget concept (e.g., 5 turns) at the start of every attempt where the agent is expected to output a structured plan but not modify any files. This is enforced by:

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

2. **Runtime enforcement** (`src/girder/agent/runtime.py`): Track whether the agent has output a plan. For the first `planning_turns` turns, any write tool call returns a synthetic held result: `"[planning phase] Write tools are not available until you have output a PLAN block."` After the plan is detected in `response.content`, write tools are unlocked. The plan text is extracted and stored as the initial scratchpad.

3. **Config key** `limits.planning_turns: int = 5` in `LimitsConfig`.

**Schema change:** `girder.toml` gains `[limits] planning_turns = 5`.

**Tests:** `test_agent_runtime.py` — verify write tools are held in planning phase; verify unlock after PLAN block; verify compaction preserves the plan.

---

### WP 8.2 — Structured Scratchpad

**Why:** The current scratchpad is free-text distilled by the model during compaction. It loses structure and the compaction call consumes tokens. A structured scratchpad is maintained by the orchestrator — not the model — and survives compaction with zero token cost.

**Implementation:**

Define a `Scratchpad` dataclass in `src/girder/agent/context.py`:

```python
@dataclass
class Scratchpad:
    plan: str                            # From planning phase
    files_read: list[str]                # Accumulated by tool registry
    files_written: list[str]             # Accumulated by tool registry
    test_results: list[str]              # From run_tests tool
    milestones: list[str]                # Injected by runtime at key turns
    prior_attempt_summary: str | None    # From retry brief (WP 8.3)
```

The `ToolRegistry` populates `files_read` / `files_written` / `test_results` on every tool execution. The scratchpad is serialized as a compact JSON block and injected as a `[TRUSTED] Scratchpad:` system message at the top of the context after the task brief. On compaction, only the scratchpad message is refreshed — the distillation model call is eliminated.

**Migration:** The existing free-text compaction in `compact()` is kept as a fallback but only fires when the structured scratchpad is unavailable (pre-8.2 data).

---

### WP 8.3 — Richer Retry Briefs

**Why:** Current retry guidance (`"Previous attempt failed: turn budget exhausted"`) provides zero actionable information. The orchestrator has rich data from the failed attempt — it should surface it.

**Implementation in `src/girder/orchestrator/task_engine.py`:**

Extract a `_build_retry_brief()` function that constructs a structured brief from the previous attempt's data:

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

This brief is passed as the `guidance` parameter to `AgentRuntime.execute_attempt()`, which injects it via `build_directive_message` in the initial task message.

**Tests:** `test_task_engine.py` — assert retry brief is constructed for turn-cap death with dirty worktree; assert it contains salvage commit reference.

---

### WP 8.4 — Codebase Index (RAG Lite)

**Why:** Agents spend 5–10 turns surveying the codebase before writing anything. A pre-computed index injected at task start would eliminate most of this.

**Implementation:**

A `CodebaseIndex` class in a new `src/girder/index/` module:

```
src/girder/index/
├── __init__.py
├── indexer.py    # Builds the index from a repo path
└── inject.py     # Formats index slices for prompt injection
```

**`indexer.py`:** Runs at task-start (before the agent's first turn), walking the worktree and building:
1. **File tree** — all paths with sizes, grouped by directory (depth-limited)
2. **Symbol map** — for each Python file: class names, function names, line numbers (via `ast.parse`)
3. **Import graph** — which modules import which (via `ast.parse` of import statements)

The index is stored as a JSON blob in the `tasks` table (new column `codebase_index_json`).

**`inject.py`:** Given the task's `scope_globs`, extracts the relevant slice of the index and formats it as a compact, token-efficient block:

```
[TRUSTED] Codebase index (relevant to your scope):

src/girder/api/
  routes.py (1320 lines) — classes: none; functions: _tier1_role, _require_run, _require_project, ...
  app.py (180 lines) — classes: none; functions: create_app, dispatch_generation, render

src/girder/db/
  repo.py (1200 lines) — functions: create_project, create_run, list_tasks_for_run, ...
  models.py (220 lines) — classes: Project, Run, Wave, Task, Attempt, RunStatus, ...
```

This replaces 5–10 `find_files` + `view_symbol_outline` calls with a single pre-computed block. The index is rebuilt on each attempt (worktree state changes).

**Config key:** `[limits] inject_index = true` (default `true`); `inject_index_max_tokens = 2000`.

---

### WP 8.5 — Intent Clarification Loop (Pre-Spec)

**Why:** Vague intents produce vague specs that agents cannot satisfy. Jules asks clarifying questions before generating a spec. Adding this as an optional pre-spec step would improve first-attempt success rates significantly.

**Flow:**

```
User submits intent
    │
    ▼
Tier-1 model: "Is this intent clear enough to generate a precise spec?"
    │
    ├── Yes → generate spec as normal
    │
    └── No → generate 2–4 clarifying questions → present in web UI
                    │
                    ▼
           User answers in-UI
                    │
                    ▼
           Enrich intent with answers → generate spec
```

**Implementation:**

1. New API endpoint: `POST /api/projects/{pid}/runs` gains an optional `clarify=true` query param.
2. New run status: `RunStatus.CLARIFYING` (pre-`DRAFT`). A new migration adds this status.
3. New FSM edge: `CLARIFYING → DRAFT`.
4. New database table: `clarification_sessions (id, run_id, questions_json, answers_json, created_at)`.
5. New web UI page: `clarify.html` — shows the questions with text inputs; `POST /api/runs/{rid}/clarify` with the answers transitions to `DRAFT` and triggers spec generation.
6. The spec generator (`src/girder/specs/generator.py`) accepts an optional `clarification: dict[str, str]` and appends it to the user intent in the system prompt.

**Config key:** `[specs] require_clarification = false` (default off — preserves current zero-friction flow).

---

## Sprint 9 — GitHub Automation Pipeline

**Goal:** Close the gap with Jules's killer feature — automated PR creation from GitHub issues. Move from "manually type intent in web UI" to "assign a GitHub issue to Girder and get a PR."

**Definition of Done:** Assigning a GitHub issue to a configured bot account creates a run; PR creation posts a comment linking to the Girder console; SC-19 (new e2e scenario) passes.

---

### WP 9.1 — GitHub App / Webhook Receiver

**Why:** Jules's number one differentiator. Any team using GitHub can integrate Girder with zero friction if it responds to issue assignments.

**New module:** `src/girder/github/webhook.py`

**Supported events:**
1. **`issues.assigned`** — When an issue is assigned to the bot account: extract issue title + body → create a run with intent = `f"{issue.title}\n\n{issue.body}"`.
2. **`issue_comment.created`** — When a comment contains `/girder <intent>` → create a run with the inline intent.
3. **`pull_request_review_comment.created`** — When a review comment contains `/girder fix this` → create a run scoped to the file in the review.

**Implementation:**

1. New FastAPI route: `POST /api/webhooks/github` — verifies HMAC-SHA256 signature against `secrets.github_webhook_secret`.
2. New `WebhookProcessor` class that dispatches to per-event handlers.
3. Each handler creates a project run via the existing `repo.create_run` → `dispatch_generation` pipeline, then posts a comment on the issue: `"Girder is working on this — [view run](http://localhost:8787/runs/{run_id})"`.
4. Secrets: add `github_webhook_secret` to `Secrets` model and `secrets.toml`.
5. Config: add `[github] webhook_enabled = false` and `github.bot_account = ""` (the GitHub username to watch for assignments).

**Security:** The webhook endpoint checks the `X-Hub-Signature-256` header before processing any payload. Invalid signatures return 403 immediately.

---

### WP 9.2 — Slack Integration

**Why:** Telegram has a small user base; most development teams use Slack. A Slack integration would dramatically increase usability.

**New module:** `src/girder/notify/slack.py`

**Features:**
- Incoming webhooks for notifications (replaces/supplements Discord)
- Slash command receiver: `/girder run "intent" in #channel` → creates a run
- Slash command: `/girder status` → lists active runs
- Interactive messages: Approve/Reject spec from Slack (Slack Block Kit interactive components)

**Implementation:**

1. `SlackNotifier` class implementing the same interface as `TelegramNotifier`.
2. New FastAPI route: `POST /api/webhooks/slack` for slash commands and interactive payloads.
3. Secrets: add `notify_slack_bot_token` and `notify_slack_signing_secret`.
4. Config: add `"slack"` to the valid `notify.channels` set.

---

### WP 9.3 — PR Template Integration

**Why:** Merged PRs should have rich descriptions automatically — what the spec said, what tasks ran, what the cost was, which tests passed.

**Implementation in `src/girder/github/client.py`:**

`create_pull_request()` already exists. Enhance it to:
1. Read `[github] pr_template` from config (already in `GithubConfig.pr_template`).
2. If set, use it as a Jinja2 template rendered with:
   - `run.id`, `run.intent`, spec tasks summary, total cost, run link
3. If not set, use a built-in template that produces a structured PR body.

**Built-in template:**
```markdown
## Summary

{{ intent }}

## Tasks completed

{% for task in tasks %}
- **{{ task.title }}** (`{{ task.task_type }}`) — {{ task.status }}
{% endfor %}

## Metrics

- Total cost: ${{ "%.4f"|format(total_cost_usd) }}
- Attempts: {{ total_attempts }}
- Run: [girder/{{ run_id[:8] }}]({{ console_url }}/runs/{{ run_id }})

---
*Generated by [Girder](https://github.com/siimliimand/Girder)*
```

---

### WP 9.4 — CI Parity Nightly Check

**Why:** The sandbox image diverges from CI over time. The current parity check (`parity` workflow) is manual/dispatch-only.

**Change to `.github/workflows/ci.yml`:**

Add a `schedule: - cron: "0 3 * * *"` trigger to the `parity` job so it runs nightly. Alert via Girder's own notification system if the parity check fails (requires the daemon to poll the workflow run status — this is already in the CI poller infrastructure).

---

## Sprint 10 — Architecture Refactor

**Goal:** Break up the three largest files (routes.py, task_engine.py, repo.py) to reduce maintenance friction and improve testability. No behavior changes.

**Definition of Done:** All existing tests pass without modification; `mypy --strict` clean; file size of any single module ≤ 600 lines.

---

### WP 10.1 — Split `src/girder/api/routes.py` (1,320 lines → 7 modules)

**Current:** Single file handling all HTTP routes, template rendering, SSE, and business logic delegation.

**Target layout:**

```
src/girder/api/
├── app.py             (unchanged — FastAPI factory)
├── deps.py            (NEW — shared dependency injection: get_db, get_settings, etc.)
├── routes/
│   ├── __init__.py    (registers all sub-routers)
│   ├── projects.py    (GET/POST /projects, /api/projects)
│   ├── runs.py        (GET /runs/{rid}, /runs/{rid}/panel, SSE /api/runs/{rid}/events)
│   ├── specs.py       (POST /api/runs/{rid}/approve|edit|regenerate)
│   ├── steering.py    (POST /api/runs/{rid}/steer, /api/runs/{rid}/reviewed|merge)
│   ├── amendments.py  (GET/POST /api/runs/{rid}/amendments/*)
│   ├── delivery.py    (GET /api/merge-queue, /merge-queue)
│   ├── history.py     (GET /api/history, /history, /runs/{rid}/postmortem)
│   └── tiers.py       (GET/POST /projects/{pid}/tier, /api/projects/{pid}/tier)
├── templates/         (unchanged)
└── static/            (unchanged)
```

**Migration strategy:**

1. Create `deps.py` with `get_db()`, `get_settings()`, `get_secrets()` FastAPI dependency functions. These replace the per-route `request.state.*` accesses.
2. Move route handlers to their sub-module, importing from `deps` and `girder.db.repo`.
3. In `routes/__init__.py`, create one `APIRouter` per module and include all into the main app.
4. The old `routes.py` is kept as a shim for one commit with `from routes import *` re-exports, then deleted.

---

### WP 10.2 — Split `src/girder/orchestrator/task_engine.py` (974 lines → 4 modules)

**Target layout:**

```
src/girder/orchestrator/
├── task_engine.py       (≤ 300 lines — top-level execute_task, retry loop only)
├── attempt_lifecycle.py (NEW — container/worktree allocation, teardown, crash handling)
├── verification.py      (NEW — test run, DiffAudit, TestSentinel, JUnit parse)
└── work_salvage.py      (NEW — Group B salvage: dirty-worktree commit on retry)
```

**Module contracts:**

- `attempt_lifecycle.py` exports `AttemptContext` (dataclass holding container_id, worktree, attempt row) and `async def allocate_attempt(...)`, `async def teardown_attempt(ctx, *, force: bool)`.
- `verification.py` exports `VerificationResult` and `async def run_verification(ctx, ...)` — runs pytest, parses JUnit XML, runs DiffAudit and TestSentinel.
- `work_salvage.py` exports `async def salvage_dirty_worktree(ctx, attempt_num) -> str | None` (returns commit SHA or None).
- `task_engine.py` imports from all three and orchestrates them in the retry loop.

---

### WP 10.3 — Split `src/girder/db/repo.py` (~1,200 lines → 7 modules)

**Target layout:**

```
src/girder/db/
├── engine.py          (unchanged)
├── models.py          (unchanged)
├── repo/
│   ├── __init__.py    (re-exports all public functions for backward compat)
│   ├── projects.py    (create_project, get_project, list_projects)
│   ├── runs.py        (create_run, get_run, update_run_fields, list_runs, etc.)
│   ├── tasks.py       (create_task, get_task, list_tasks_for_run/wave, etc.)
│   ├── events.py      (insert_event, get_latest_event, list_events_for_run)
│   ├── integrity.py   (insert_integrity_violation, list_integrity_violations_for_run)
│   ├── steering.py    (insert_steering_event, consume_steering_events, set_run_paused)
│   └── usage.py       (insert_token_usage, update_token_usage_actual, sum_token_usage_for_run)
```

**Migration strategy:** The `repo/__init__.py` does `from .projects import *; from .runs import *; ...` so all existing call sites (`from girder.db import repo; repo.create_run(...)`) continue to work unchanged. The import path is the only refactor needed.

---

### WP 10.4 — Replace `assert` with Explicit Guards

**Why:** `assert fresh is not None` is stripped with Python `-O` and gives unhelpful errors in production.

**Grep target:** `assert .* is not None` in `src/girder/orchestrator/run_engine.py` (10+ instances).

**Replacement pattern:**

```python
# Before:
fresh = await repo.get_run(self.db, run.id)
assert fresh is not None

# After:
fresh = await repo.get_run(self.db, run.id)
if fresh is None:
    raise RuntimeError(f"run {run.id} disappeared during pump — database consistency error")
```

All instances replaced in: `run_engine.py`, `task_engine.py`, `scheduler.py`, `integrator.py`.

---

### WP 10.5 — TypedDict Payloads for Known Event Shapes

**Why:** `dict[str, Any]` payloads lose type safety at the call site. The FSM and repo layer pass structured payloads that should be type-checked.

**Implementation:**

Add `src/girder/db/payloads.py` with `TypedDict` definitions:

```python
from typing import TypedDict

class TransitionPayload(TypedDict, total=False):
    reason: str
    entity: str
    from_: str
    to: str

class SteeringPayload(TypedDict, total=False):
    directive: str
    task_id: str

class BudgetEventPayload(TypedDict):
    role: str
    model: str
    est_cost_usd: float
    remaining_usd: float
```

Annotate the most-called repo functions first. `mypy --strict` will surface remaining `Any` gaps.

---

## Sprint 11 — Multi-Stack Support

**Goal:** Support JavaScript/TypeScript and Go projects in addition to Python. This expands the addressable use-case by 5× and is required before Girder can be used on non-Python codebases.

**Definition of Done:** SC-20 (JS/TS e2e scenario against a new `tests/fixtures/e2e-target-ts/` fixture) passes; SC-01 (Python) still passes; `mypy --strict` clean.

---

### WP 11.1 — Stack Plugin Architecture

**Why:** Currently the stack is hardcoded in: `view_symbol_outline` (Python AST), `verify_cmd` (pytest), `Dockerfile.python-3.12`, the `stack` config key. A stack plugin system makes the code extensible.

**New module:** `src/girder/stacks/`

```
src/girder/stacks/
├── __init__.py        (StackPlugin ABC + registry)
├── python.py          (existing Python-3.12 behavior)
├── node.py            (NEW — Node.js / TypeScript)
└── golang.py          (NEW — Go)
```

**`StackPlugin` ABC:**

```python
from abc import ABC, abstractmethod
from pathlib import Path

class StackPlugin(ABC):
    name: str          # e.g. "python-3.12", "node-20", "go-1.23"
    
    @abstractmethod
    def runner_image(self) -> str:
        """Docker/Podman image tag for this stack's runner."""
    
    @abstractmethod
    def test_command(self, python_bin: str | None = None) -> list[str]:
        """Command to run the full test suite and produce JUnit XML."""
    
    @abstractmethod
    def symbol_outline_command(self, path: str) -> list[str]:
        """In-container command to list symbols in a source file."""
    
    @abstractmethod
    def test_signal_patterns(self) -> list[str]:
        """Glob patterns for test files (Layer-2 hash manifest)."""
    
    @abstractmethod
    def package_cache_mounts(self) -> dict[str, str]:
        """Host path → container path for read-only package cache mounts."""
```

**Registry:** `STACK_REGISTRY: dict[str, StackPlugin]` populated at import time. `load_settings()` validates `project.stack` against the registry keys.

---

### WP 11.2 — Node.js / TypeScript Stack

**New file:** `src/girder/stacks/node.py`

**New file:** `container/Dockerfile.node-20`

```dockerfile
FROM node:20-slim
WORKDIR /workspace
RUN npm install -g typescript jest ts-jest @jest/junit-reporter
# Cache dirs pre-populated by cache-warm.sh
ENV NPM_CONFIG_CACHE=/cache/npm
```

**`NodePlugin` implementation:**

```python
class NodePlugin(StackPlugin):
    name = "node-20"
    
    def runner_image(self) -> str:
        return "girder-runner:node-20"
    
    def test_command(self, **kwargs) -> list[str]:
        # Produces junit XML via jest-junit reporter
        return ["npx", "jest", "--ci", "--reporters=jest-junit",
                "--env=JEST_JUNIT_OUTPUT_FILE=/workspace/.girder-verify.xml"]
    
    def symbol_outline_command(self, path: str) -> list[str]:
        # Use node -e with acorn or @typescript-eslint/parser
        return ["node", "-e", _TS_OUTLINE_SNIPPET, path]
    
    def test_signal_patterns(self) -> list[str]:
        return ["**/*.test.ts", "**/*.test.js", "**/*.spec.ts",
                "**/*.spec.js", "jest.config.*", "vitest.config.*"]
    
    def package_cache_mounts(self) -> dict[str, str]:
        return {"/var/cache/orchestrator/npm": "/cache/npm"}
```

`_TS_OUTLINE_SNIPPET`: a Node.js one-liner using `@typescript-eslint/parser` to list exported functions and classes with line numbers.

**JUnit XML parsing:** The existing `parse_junit_xml` in `orchestrator/baseline.py` is format-agnostic. Jest with `jest-junit` produces compatible XML. No changes needed.

---

### WP 11.3 — Go Stack

**New file:** `src/girder/stacks/golang.py`  
**New file:** `container/Dockerfile.go-1.23`

```python
class GoPlugin(StackPlugin):
    name = "go-1.23"
    
    def runner_image(self) -> str:
        return "girder-runner:go-1.23"
    
    def test_command(self, **kwargs) -> list[str]:
        return ["go", "test", "./...", "-v",
                f"-junit-report=/workspace/.girder-verify.xml"]
        # Uses gotestsum for JUnit output when available
    
    def symbol_outline_command(self, path: str) -> list[str]:
        return ["go", "doc", "-all", path]
    
    def test_signal_patterns(self) -> list[str]:
        return ["**/*_test.go"]
    
    def package_cache_mounts(self) -> dict[str, str]:
        return {"/var/cache/orchestrator/go": "/root/go/pkg/mod"}
```

---

### WP 11.4 — Symbol Outline: Phase 2 (tree-sitter)

Replace the `_AST_OUTLINE_SNIPPET` and `grep -nE` fallback in `tools.py` with per-stack `symbol_outline_command()` from the stack plugin. Each stack plugin can use its native toolchain.

For Python: Keep `ast.parse` (already in the runner image). For JS/TS: Use `@typescript-eslint/parser` (in the Node runner image). For Go: Use `go doc`. For other: Fall back to `grep -nE "^(def|class|function|func|pub fn)\b"`.

---

## Sprint 12 — Web Console & Observability

**Goal:** Transform the minimal functional console into a developer-grade UI, and add the observability infrastructure needed to tune the system data-driven.

**Definition of Done:** All existing HTML/SSE behavior preserved; Prometheus metrics endpoint active; dark mode implemented; mypy clean.

---

### WP 12.1 — Dark Mode & Design System

**Why:** The current CSS is 185 lines of light-mode functional styling. Every serious developer tool has dark mode.

**Implementation in `src/girder/api/static/style.css`:**

1. Add CSS custom properties (variables) for all colors:
```css
:root {
  --bg:          #fafaf8;
  --bg-surface:  #f2f2ef;
  --border:      #e0e0dc;
  --text:        #1c1c1c;
  --text-muted:  #6b6b6b;
  --accent:      #0b5fa5;
  --accent-dark: #094c84;
  --red:         #c02020;
  --amber:       #d99a1a;
  --green:       #2c8a3c;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg:          #111;
    --bg-surface:  #1e1e1e;
    --border:      #333;
    --text:        #e8e8e8;
    --text-muted:  #888;
    --accent:      #4da3ff;
    --accent-dark: #6bb8ff;
    --red:         #ff6b6b;
    --amber:       #ffc845;
    --green:       #5cbf70;
  }
}
```

2. Replace all hardcoded hex colors in the existing rules with `var(--...)` references.
3. Add a manual toggle button in `base.html` that sets `data-theme="dark"` on `<html>` and persists to `localStorage`.

---

### WP 12.2 — Live DAG with Status Updates

**Why:** The task DAG is currently rendered server-side as static HTML and only refreshes on panel poll (every 2s). It should update live via SSE without a full-panel re-fetch.

**Implementation:**

The existing SSE stream in `routes.py` already emits `state_transition` events with `entity=task`. Add a JavaScript handler in `run.html`:

```javascript
es.addEventListener("state", function(e) {
    var d = JSON.parse(e.data);
    if (d.payload && d.payload.entity === "task") {
        var taskId = d.payload.id;
        var newStatus = d.payload.to;
        var el = document.querySelector('[data-task-id="' + taskId + '"]');
        if (el) {
            // Update CSS class and badge text
            el.className = el.className.replace(/task-\w+/, "task-" + _statusClass(newStatus));
            var badge = el.querySelector('.task-status-badge');
            if (badge) badge.textContent = newStatus;
        }
    }
});
```

Add `data-task-id="{{ t.id }}"` attributes to each task `<div>` in `run.html` and separate `class="task-status-badge"` for the status text.

---

### WP 12.3 — Prometheus Metrics Endpoint

**Why:** Data-driven tuning requires metrics. Without Prometheus, tuning is based on reading logs and anecdotes.

**New module:** `src/girder/api/metrics.py`

**Endpoint:** `GET /metrics` (Prometheus text format)

**Metrics to expose:**

| Metric | Type | Labels | Description |
|---|---|---|---|
| `girder_runs_total` | Counter | `status` | Runs by terminal status |
| `girder_tasks_total` | Counter | `status`, `task_type` | Tasks by outcome |
| `girder_attempts_total` | Counter | `status` | Attempts by outcome |
| `girder_attempt_turns` | Histogram | — | Turns used per attempt |
| `girder_attempt_cost_usd` | Histogram | `model_role` | Cost per attempt |
| `girder_scope_violations_total` | Counter | `kind` | Violations by type |
| `girder_model_latency_seconds` | Histogram | `model_role`, `provider` | Model call latency |
| `girder_budget_denials_total` | Counter | — | Pre-flight budget denials |
| `girder_active_runs` | Gauge | — | Currently-running runs |
| `girder_active_containers` | Gauge | — | Live sandbox containers |

**Implementation:** Pure Python using string formatting (no `prometheus_client` dependency — it's a 10KB text format). Query SQLite on each `/metrics` request with lightweight aggregation queries.

---

### WP 12.4 — Structured JSON Logging

**Why:** The codebase uses `logging.getLogger()` throughout but defaults to text format. Structured JSON logs are essential for log aggregation (Loki, Datadog, CloudWatch).

**Implementation:**

Add `src/girder/logging.py`:

```python
import json, logging, time

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "ts":      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level":   record.levelname,
            "logger":  record.name,
            "msg":     record.getMessage(),
            "module":  record.module,
            **({"exc": self.formatException(record.exc_info)} if record.exc_info else {}),
        })
```

Add CLI flag `--log-format json|text` (default `text` for human use, `json` for daemon mode).

---

### WP 12.5 — API Authentication

**Why:** The console currently has zero authentication. Anyone with network access to port 8787 can approve specs and trigger merges.

**Implementation:**

1. New config key: `[web] api_key = ""` (empty = no auth, for localhost-only use).
2. If `api_key` is set, all API endpoints require `Authorization: Bearer <key>` or `X-API-Key: <key>` header. Web UI pages read the key from `localStorage` and include it in all fetch/form requests.
3. The key is set via `girder web --api-key <key>` or the env var `GIRDER_WEB__API_KEY`.
4. `secrets.toml` can hold `[web] api_key = "..."`.

This is intentionally minimal — not OAuth, not sessions. Full auth is out of scope per plan.md §2.2.

---

### WP 12.6 — Diff Viewer Improvements

**Why:** The current diff display is a `<pre>` block with raw unified diff text — no syntax highlighting, no line numbers, no split view.

**Implementation:**

Replace the `<pre>{{ diff }}</pre>` blocks in `run.html` and `postmortem.html` with a lightweight diff renderer:

1. Add `src/girder/api/static/diff.js` — a 100-line script that takes a unified diff string and renders it as a two-column table with `+` lines in green and `-` lines in red, line numbers on the left.
2. No external dependencies. Uses `<table>` + CSS classes.

---

## Sprint 13 — Self-Hosting & Productionisation

**Goal:** Girder processes its own GitHub issues. This is the north-star validation that all the above improvements work end-to-end.

**Definition of Done:** At least one non-trivial Girder improvement (≥ 3 files changed) was implemented by Girder itself; the resulting PR was opened, CI passed, and it was merged.

---

### WP 13.1 — Production Deployment Docs

**New file:** `docs/deployment.md`

Covers:
- Systemd service unit for `girder daemon`
- Reverse proxy (nginx/caddy) in front of the web console for external access
- Secrets management with `gnome-keyring` or `pass`
- Backup strategy for `~/.local/share/girder/girder.db`
- Log rotation for structured JSON logs
- GitHub App setup (webhook URL, permissions, installation)

---

### WP 13.2 — Database Maintenance Commands

**Why:** `~/.local/share/girder/girder.db` grows unbounded. Old runs accumulate events, tool_calls, and token_usage rows indefinitely.

**New CLI command:** `girder prune [--older-than-days N] [--dry-run]`

Deletes runs (and all cascading rows via FK) older than N days with terminal status (`merged`, `failed`, `aborted`). Defaults to 90 days. Prints a summary of what would be deleted in `--dry-run` mode.

**New migration:** `014_prune_indexes.sql` — adds `(status, created_at)` index on `runs` for efficient pruning queries.

---

### WP 13.3 — `girder validate` CLI Command

**Why:** Users misconfigure girder.toml silently. A validate command catches all errors before the first run.

**New CLI command:** `girder validate [--config-path PATH]`

Checks:
1. All required fields present in `girder.toml`
2. Model roles: all three tiers present, prices > 0
3. `project.stack` is a known stack
4. Secrets file exists and is mode 0600
5. API keys are non-empty for configured providers
6. Sandbox runtime (podman/docker) is available in PATH
7. Runner image exists: `podman images -q girder-runner:<stack>`
8. `project.test_directories` all exist in the repo

Exits 0 on success, 1 on any failure. Prints human-friendly error messages.

---

### WP 13.4 — Girder-on-Girder Self-Hosting

**Changes to `girder.toml`** (the one at the Girder repo root):

1. Enable webhook processing: `[github] webhook_enabled = true`, `bot_account = "girder-bot"`.
2. Create the GitHub App and configure the webhook URL.
3. Add the Girder repo as a project via the web console.
4. Label issues with `girder-ready` to gate which issues Girder will pick up automatically.

**North-star metric:** Track `self_hosting_success_rate` — ratio of self-processed Girder issues that produced a merged PR without human amendment.

---

## Resolution Log

| # | Decision | Rationale |
|---|---|---|
| R-SP7-1 | `edit_file` uses line numbers, not context matching | Line numbers are unambiguous; context matching fails on duplicate code |
| R-SP7-2 | `search_symbols` Phase 1 uses ripgrep composition, not tree-sitter | Ships faster; tree-sitter added in Sprint 11 with stack plugins |
| R-SP8-1 | Planning phase enforced by the orchestrator (held tool calls), not by prompt alone | Prompt-only enforcement is a speed bump; orchestrator enforcement is mechanical |
| R-SP8-4 | Codebase index is rebuilt per-attempt, not cached | Worktree state changes between attempts; stale index is worse than no index |
| R-SP9-1 | GitHub App over OAuth App | App tokens are repo-scoped; OAuth tokens are user-scoped — App is safer |
| R-SP10-1 | `repo.py` split via re-export `__init__.py` | Zero call-site changes; backward compatible migration |
| R-SP11-1 | Stack plugins are Python classes, not TOML/YAML config | Arbitrary test commands need code; declarative config can be layered on later |
| R-SP12-5 | API key auth, not sessions/OAuth | Minimal surface; full auth is explicit non-goal in plan.md §2.2 |
| R-SP13-4 | Self-hosting gated by `girder-ready` label | Prevents Girder from processing every typo/question issue in its own repo |

---

## Appendix: Sprint Summary Table

| Sprint | Focus | New Files | Changed Files | New Tests | Effort |
|---|---|---|---|---|---|
| 7 | Agent tools | `tools.py` additions | `tools.py`, `TOOL_SCHEMAS` | 20 | 1 week |
| 8 | Agent intelligence | `index/`, prompts | `prompts.py`, `runtime.py`, `context.py`, `task_engine.py` | 30 | 2 weeks |
| 9 | GitHub automation | `webhook.py`, `slack.py` | `client.py`, `routes.py`, `ci.yml` | 15 | 2 weeks |
| 10 | Architecture refactor | `api/routes/`, `db/repo/`, `attempt_lifecycle.py`, `verification.py`, `work_salvage.py` | Many (split only) | 5 | 1.5 weeks |
| 11 | Multi-stack | `stacks/`, `Dockerfile.node-20`, `Dockerfile.go-1.23` | `tools.py`, `config.py`, `task_engine.py` | 25 | 3 weeks |
| 12 | Console & observability | `metrics.py`, `logging.py`, `diff.js` | `style.css`, `routes.py`, `cli.py`, templates | 10 | 1.5 weeks |
| 13 | Self-hosting | `docs/deployment.md` | `girder.toml`, `cli.py`, `routes.py` | 5 | 1 week |
| **Total** | | **~20 new files** | **~30 touched** | **~110 new tests** | **~12 weeks** |
