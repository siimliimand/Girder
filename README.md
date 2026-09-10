# Girder

**Personal AI development orchestrator: describe a feature, approve a spec, and let sandboxed agents implement, test, verify, and deliver it to `main`.**

Girder is a local-first, single-user autonomous software delivery system. You describe a desired feature or bugfix in natural language; the system produces a structured, validated specification (OpenSpec), acquires your explicit approval, and then orchestrates AI agents inside isolated rootless containers to implement, test, verify, and merge the code — with every mechanical invariant enforced *outside* the agent's reach.

- **Specification:** [`docs/plan.md`](docs/plan.md) (v2.2 — the source of truth for intent)
- **Engineering blueprint:** [`docs/implementation-plan.md`](docs/implementation-plan.md) (technology picks, DDL, state machines, module contracts)

Status: **Sprints 1–6 complete** — infrastructure & sandboxing, spec engine & approval, sequential autonomous loop, GitHub delivery pipeline with tiered merge, wave-based concurrency, and the live steering console. 546 tests green.

---

## Table of Contents

- [Why Girder](#why-girder)
- [How it works](#how-it-works)
- [The trust model](#the-trust-model)
- [Autonomy tiers](#autonomy-tiers)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Quick start](#quick-start)
- [CLI reference](#cli-reference)
- [Web console & HTTP API](#web-console--http-api)
- [What agents can and cannot do](#what-agents-can-and-cannot-do)
- [Waves: parallel execution](#waves-parallel-execution)
- [Safety rails](#safety-rails)
- [Project layout](#project-layout)
- [Development](#development)
- [License](#license)

---

## Why Girder

Most "AI writes code" demos stop at a green test in a throwaway sandbox. Girder is built for the last mile: **unattended delivery to your real repository**. That changes the threat model completely — an agent that can merge to `main` can also delete your tests, exfiltrate a credential it saw in a stack trace, or follow a prompt-injection payload planted in a README. Girder's answer is a set of architectural stances:

1. **Mechanical invariants over prompt guidelines.** Trusting an LLM to "not edit tests" or "self-police" leads to silent failure. Test integrity, file boundaries, and budget limits are enforced by the container boundary and the orchestrator process — never by anything the agent's own shell can touch.
2. **Enforcement lives outside the agent's blast radius.** Anything inside the guest (in-guest `chmod`, a git hook in its own worktree, an env var) is treated as a speed bump, not a guarantee. The guarantee is the read-only bind mount, the orchestrator-side diff audit, and the deterministic state machine.
3. **Specs are immutable contracts.** Once you click Approve, the OpenSpec document is frozen and SHA-256 anchored. Agents cannot rewrite specs to mark their own work complete — and `openspec/**` is a protected path for them. If a spec turns out to be impossible, there is a formal amendment protocol instead of hallucinated workarounds.
4. **Fail-fast local verification.** GitHub CI is the final audit stamp, not the inner debugging loop. Agents run against a containerized local test runner mirroring the CI environment; 100% of tests execute locally before any push.
5. **Resilience through ephemerality.** Worktrees, containers, and agent sessions are disposable; orchestrator state (SQLite, WAL) and git commit trees are eternal. The system can be killed with `kill -9` at any microsecond and recovers at the exact task boundary on reboot.
6. **Trust is earned, not assumed.** No project defaults to unattended writes to `main`. Autonomy is a per-project dial (T0 supervised → T1 auto-merge+notify → T2 full autonomy) that graduates on a clean-merge streak, and demotes instantly.

## How it works

```
 you: "add rate limiting to the API"
   │
   ▼
 Spec Engine          Tier-1 model drafts an OpenSpec change proposal
   │                  (tasks, file scopes, success criteria) → structural
   │                  validation → you Approve / Edit / Regenerate in the UI
   ▼
 Freeze               spec committed to run/<run-id>, SHA-256 recorded
   │
   ▼
 Baseline             full test suite on main (per-test flake status recorded)
   │
   ▼
 Waves                tasks → dependency DAG → mechanical overlap demotion →
   │                  concurrent worktree agents (sandboxed, network=none)
   │
   ▼
 Serialized           each task merged ff-only into the run branch — gated by
 integration          orchestrator-side diff audit + full local suite per merge
   │
   ▼
 Push + PR            branch pushed, PR opened from spec metadata
   │
   ▼
 CI poller            green → conformance review; red → diagnostic fix agent
   │                  (≤3 attempts), baseline-aware flaky circuit breaker
   ▼
 Conformance          Tier-1 model reviews diff vs frozen spec
   │
   ▼
 Merge gate           orchestrator-side audit must be clean
   │
   ├── T0: PR parks at merge-pending-human — you click merge
   └── T1/T2: auto-merge (squash/rebase/merge) + notification
```

Crash at any point? On restart the daemon runs boot reconciliation: in-flight attempts are marked crashed, their worktrees pruned, and the run resumes from the last clean commit — completed tasks are never re-executed.

## The trust model

The single most important idea in Girder: **the agent is never trusted until the orchestrator independently verifies it.** The enforcement stack for test integrity (the "silent test evisceration" attack — an agent deleting assertions to force green) is layered so no single bypass defeats it:

| Layer | Mechanism | Where it lives | Bypass path from inside the guest |
|---|---|---|---|
| 1 | Read-only bind mount of a pristine test snapshot (materialized via `git archive` from the base commit) shadowing the worktree's test dir; container runs with `--cap-drop=ALL`, `no-new-privileges` | Host, before container start | **None** — no `CAP_SYS_ADMIN`, so no remount path, even as in-guest root |
| 2 | Content-hash manifest over `test_signal_patterns` captured at task start, re-hashed at task end | Orchestrator process | **None** — the agent never runs as the orchestrator |
| 3 | `DiffAudit` on the final commit — name-only diff vs scope globs, test patterns, and protected paths — refuses every merge that fails it | Orchestrator process, inside `audit_gated_merge` | **None** |
| advisory | In-worktree pre-commit warning on test-path writes | Guest | `git commit --no-verify` — acceptable; never the actual gate |

Integrity violations (any layer) fail the attempt **without retry**, are logged distinctly from ordinary test failures, reset the project's clean-merge streak, and block merge at every tier.

Other pillars:

- **Scope guard on every tool call.** Writes outside the task's declared `scope_globs` are held, not executed, and recorded as `scope_violation`. Reads inside the worktree are allowed-and-logged (agents need dynamic discovery), except `protected_read_paths` — `.github/**`, `openspec/**`, `.env*`, credential-shaped files — which are *always* violations. Set `strict_read_scope = true` to hold any out-of-scope read.
- **Repository content is data, never instructions.** Every tool result is wrapped in an `<untrusted-data>` block; the system prompt states plainly that directive-sounding text in files, comments, or logs is not to be followed. This matters because the pipeline ends in writes to `main`.
- **Secret redaction everywhere.** A pattern set (AWS keys, GitHub tokens, JWTs, PEM blocks, connection strings, generic `api_key=` assignments with an entropy floor) plus a per-project env-var denylist runs between every raw tool output and (a) SQLite, (b) notifications, (c) the SSE console. Raw secrets are never persisted or transmitted; every substitution writes an audit row.
- **Bounded everything, checked before spending.** Pre-flight cost estimation runs before each model call — if the estimate exceeds remaining budget, the call is *never dispatched*. Actuals are reconciled after the response and re-checked. On top of the dollar cap: 600 s wall-clock per attempt, 20 turns per attempt, 3 attempts per task, 3 CI fix attempts.
- **Zero host secrets in containers.** Container environments are constructed explicitly; your secrets file is never mounted, and the GitHub token travels only inside the orchestrator process (via a temp `GIT_ASKPASS` at push time) — never into containers, git config, or argv.

## Autonomy tiers

Every project starts supervised. Promotion is earned; demotion is instant.

| Tier | Behavior |
|---|---|
| **T0 — Supervised** *(default)* | The pipeline runs through CI green, conformance review, and the diff audit — then stops at a fully-green PR and notifies you. You click merge. |
| **T1 — Auto-merge, notify** | Merges automatically on all-green + clean audit, sends a rich notification per merge, and pauses new runs when the rolling review window fills (default: 3 unreviewed merges). Mark runs reviewed with `girder review <run-id>` or the UI. |
| **T2 — Full autonomy** | Merges silently (still logged). Only offered after the project accumulates a clean-merge streak ≥ `t2_required_streak` (default: 10) at T1 with zero integrity violations. |

Any integrity violation, catastrophic conformance deviation, or *newly introduced* test flakiness escalates to human review **regardless of tier**. Flake status is compared against the baseline run on `main`: a test that only became flaky on the agent's branch is treated as a regression signal, not noise.

## Architecture

One `asyncio` daemon owns everything: the FSM pump, sandbox supervision, worktree GC, CI pollers, Telegram inbound, and the web console.

```
┌────────────────────────────────────────────────────────────────────┐
│ Web console (FastAPI + Jinja2 + HTMX + SSE)  ·  127.0.0.1:8787     │
│  spec review · live tool-call stream · steering · spend · tiers    │
└──────────────────────────────┬─────────────────────────────────────┘
                               │
┌──────────────────────────────▼─────────────────────────────────────┐
│ Orchestrator daemon — deterministic FSM, the sole trust boundary   │
│                                                                    │
│  spec engine      wave/task engine      git & worktree manager     │
│  budget guard     diff audit + hashes   redaction pipeline         │
│  scope guard      recovery service      GC daemon                  │
└───────┬──────────────────┬──────────────────────┬──────────────────┘
        │                  │                      │
┌───────▼────────┐  ┌──────▼─────────┐   ┌────────▼────────┐
│ Agent runtime  │  │ GitHub client  │   │ Notifier        │
│ turn loop,     │  │ push / PR /    │   │ Telegram /      │
│ tools,         │  │ checks / merge │   │ Discord, always │
│ compaction     │  │                │   │ redacted        │
└───────┬────────┘  └────────────────┘   └─────────────────┘
        │
┌───────▼────────────────────────────────────────────────────────────┐
│ Execution sandbox (rootless Podman/Docker)                         │
│  · worktree bind: RW      · test snapshot bind: RO (Layer 1)       │
│  · package cache binds: RO · --cap-drop=ALL · network=none         │
│  · pids/memory/cpus limits  · zero host secrets                    │
└────────────────────────────────────────────────────────────────────┘
```

State is persisted in SQLite (WAL, `BEGIN IMMEDIATE`, single writer) in a strict hierarchy — `Project → Run → Wave → Task → Attempt`, with `AgentEvent`, `TokenUsage`, `ToolCall`, and `RedactionLog` rows per attempt. Every state transition funnels through one guarded function that validates the edge and writes the row update **and** its audit event in the same transaction — state is committed *before* any external action (container start, git push, notification). Illegal edges raise; there is no code path that mutates a status column directly.

Models are addressed by **capability role, not model name** (D7): `tier1` for spec generation / conformance / conflict resolution, `tier2` for standard implementation, `tier3` for log analysis and git ops. Any OpenAI-compatible chat-completions endpoint works (OpenRouter or direct Anthropic/OpenAI); model IDs and prices are pure config and feed the budget guard.

## Requirements

- **Linux** (developed on bare-metal Linux; anything that runs rootless containers works)
- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/)** for dependency management
- **git** (worktrees, branches, audits)
- **Rootless Podman** (or Docker — `sandbox.runtime` accepts either; the flag surface is identical)
- **Model API access** — an OpenRouter API key, or direct Anthropic/OpenAI keys, depending on your role config
- **A GitHub fine-grained PAT** (contents: read/write, checks: read/write, PRs: read/write) — only needed for the delivery pipeline

## Installation

```bash
git clone https://github.com/siimliimand/Girder.git
cd Girder
uv sync
```

Build the runner image for your stack (the image mirrors the GitHub Actions runner for CI parity and pre-bakes the test toolchain so `--network=none` testing works):

```bash
podman build -t girder-runner:python-3.12 -f container/Dockerfile.python-3.12 container/
# or: docker build -t girder-runner:python-3.12 -f container/Dockerfile.python-3.12 container/
```

Verify the install:

```bash
uv run girder --version
uv run girder migrate
```

## Configuration

Two files, strictly separated: **project config** (committed, no secrets) and **orchestrator secrets** (host-only, never committed, never mounted into containers).

### `girder.toml` — per project, at the repo root

Girder finds it by walking up from the working directory. Every key is optional; defaults are shown below.

```toml
[project]
name = "myapp"
stack = "python-3.12"                      # selects the girder-runner:<stack> image
test_directories = ["tests"]
allow_empty_baseline = false               # true ⇒ pytest "no tests collected" (exit 5)
                                           # is a green baseline — for test-less repos;
                                           # a crashed conftest still escalates
test_signal_patterns = [                   # Layer-2 hash manifest coverage
  "tests/**", "**/test_*.py", "**/*_test.py", "**/conftest.py",
]
protected_read_paths = [                   # agent reads here are ALWAYS violations
  ".github/**", "openspec/**", ".git/**", ".env*", "**/*.pem", "**/*credentials*",
]
strict_read_scope = false                  # true ⇒ any out-of-scope read is held

[autonomy]
tier = 0                                   # 0 supervised (default) | 1 | 2
t2_required_streak = 10                    # clean T1 merges before T2 is offerable
t1_review_window = 3                       # pause runs after N unreviewed merges

[budget]
run_cap_usd = 5.00                         # hard financial stop per run

[limits]
attempt_wallclock_s = 600
attempt_max_turns = 20
task_max_attempts = 3
ci_fix_attempts = 3
conflict_resolution_attempts = 2
tool_output_max_lines = 100
tool_output_max_tokens = 4000

[sandbox]
runtime = "podman"                         # podman | docker
network = "none"                           # none | private
memory = "4g"
cpus = 2.0
pids_limit = 512
python_bin = "python3"                     # interpreter for baseline/verify suites

[github]
remote = "origin"
api_url = "https://api.github.com"         # override for proxies/tests
poll_interval_s = 30.0                     # CI checks poll cadence
poll_timeout_s = 3600.0                    # exceeded ⇒ escalate, never poll forever
merge_method = "squash"                    # merge | squash | rebase

[notify]
channels = []                              # subset of ["telegram", "discord"]
# telegram_chat_id = "…"                   # non-secret routing info may live here

[web]
host = "127.0.0.1"                         # loopback-only by default
port = 8787

# Model roles — the role is the architectural commitment; the model ID is
# deployment config. All three roles are required as a set.
[[models.roles]]
role = "tier1"                             # spec gen, conformance, conflict resolution
provider = "openrouter"                    # openrouter | anthropic | openai
model = "<frontier-reasoning-model-id>"
context_window = 200000
max_output_tokens = 8192
price_in_per_mtok = 0.0                    # real prices feed the budget guard
price_out_per_mtok = 0.0

[[models.roles]]
role = "tier2"                             # standard code implementation
provider = "openrouter"
model = "<fast-frontier-model-id>"

[[models.roles]]
role = "tier3"                             # log analysis, git ops
provider = "openrouter"
model = "<high-efficiency-model-id>"
```

Environment overrides use the `GIRDER_` prefix with `__` nesting and always win over the TOML file, e.g. `GIRDER_BUDGET__RUN_CAP_USD=9.5`.

### `~/.config/girder/secrets.toml` — host-only, mode `0600`

```toml
[models]
openrouter_api_key = "…"                   # or anthropic_api_key / openai_api_key

[github]
token = "github_pat_…"                     # fine-grained PAT

[notify]
telegram_bot_token = "…"
discord_webhook_url = "…"

[redaction]                                # env var NAMES to redact from output
secret_env_names = ["GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "…"]
```

A permissive secrets file is **refused**, not warned about — run `chmod 600 ~/.config/girder/secrets.toml`. Secret values never appear in any log, `repr()`, database row, notification, or container environment (unit-tested).

## Quick start

```bash
# 1. From the repo you want Girder to work on (it looks for girder.toml
#    walking up from the CWD), apply the schema:
girder migrate

# 2. Serve the console and register your project (name + repo path) at
#    http://127.0.0.1:8787
girder web

# 3. Submit an intent on the project page ("add rate limiting to the API").
#    The spec engine drafts an OpenSpec proposal; review it and click Approve.

# 4. Run the orchestrator. The daemon reconciles any prior crash, keeps the
#    worktree GC alive, pumps every pumpable run (concurrently, across all
#    projects), and serves Telegram amendment inbound if configured:
girder daemon
```

The daemon drives the run through baseline → waves → integration → push → PR → CI → conformance → merge gate. At T0 it parks at a ready PR and notifies you; click merge (or use the merge queue page). At T1/T2 it merges itself.

Prefer step-by-step? Pump a single run one FSM step at a time, or straight through:

```bash
girder pump <run-id>            # one step
girder pump <run-id> --wait     # drive to a terminal state
```

Other operational commands:

```bash
girder recover                  # run boot reconciliation and print the report
girder gc --once                # single worktree GC pass (prune/quarantine/disk alert)
girder review <run-id>          # mark a merged run human-reviewed (T1 review window)
```

## CLI reference

```
girder [--db PATH] [--migrations-dir DIR] [-v] <command>

  migrate            Apply pending migrations and exit
  recover            Run boot reconciliation and print the report
  gc [--once]        Run the worktree garbage collector (loop or single pass)
  daemon             Orchestrator daemon: recovery + GC + run-engine pump
      --sandbox podman|local   `local` executes attempts directly on the host
                               with NO isolation — requires --dev or
                               GIRDER_DEV=1; NOT a security boundary
      --dev                    confirm a development environment
  pump <run_id>      Drive one run a single pump step (--wait: to completion)
      --sandbox, --dev          same as daemon
  web [--host H] [--port P]    Serve the approval web console
  review <run_id>    Mark a merged run as human-reviewed (T1 review window)
```

`--db` defaults to `~/.local/share/girder/girder.db`. The daemon and `pump` both refuse the local sandbox outside an explicit dev opt-in, with a readable error.

## Web console & HTTP API

Pages: **Projects** · **Run detail** (task DAG with state colors, live redacted tool-call stream via SSE, diff viewer) · **Spec review** (proposal, file-impact table, cost estimate, Approve/Edit/Regenerate) · **Amendments inbox** · **Merge queue** (T0) · **Spend dashboard** (projected pre-flight vs reconciled actual) · **Tier console** (streak view, promote/demote — promotion offered only when earned) · **History** · **Post-mortem explorer** (per-attempt prompts, tool-call timeline, token consumption — historical views pass through the same redactor as live ones).

Integrity and scope violations stream as a **distinct event class**, so "the code was wrong" is visually separable from "the agent left its boundary."

REST endpoints (JSON where not noted):

```
POST /api/projects                              {name, repo_path}
GET  /projects/{pid}                            (page)
POST /api/projects/{pid}/runs                   {intent}
GET  /api/runs/{rid} · /runs/{rid} (page) · /runs/{rid}/panel (HTMX)
GET  /api/runs/{rid}/events                     (SSE live stream)
GET  /api/runs/{rid}/graph · /api/runs/{rid}/spend · /api/runs/{rid}/amendments
POST /api/runs/{rid}/approve | edit | regenerate
POST /api/runs/{rid}/amendments/{aid}/approve | reject | abort
POST /api/runs/{rid}/steer                      {pause|resume|abort|inject|skip|force_pass}
POST /api/runs/{rid}/reviewed · /api/runs/{rid}/merge
GET  /api/merge-queue · /merge-queue (page)
POST /api/projects/{pid}/tier                   · /projects/{pid}/tier (page)
GET  /api/history · /history (page) · /runs/{rid}/postmortem (page)
```

Steering is mid-flight and safe: `pause` suspends at the current task boundary, `inject` appends a user directive that is tagged **trusted** in the prompt (distinct from untrusted repo content), `abort` kills containers, removes worktrees, and resets the branch.

## What agents can and cannot do

Each attempt runs a thin custom runtime (no LangChain, no framework magic): turn loop → model call → scope-check every tool call → execute allowed ones in-container → redact outputs → append to context → compact at 70% of the window (system prompt, frozen spec slice, and scratchpad retained; raw tool outputs purged).

| Tool | Scope rule |
|---|---|
| `read_file`, `find_files`, `ripgrep`, `view_symbol_outline` | reads: allowed-and-logged inside the worktree; `protected_read_paths` always held |
| `write_file`, `apply_patch` | **held if the target falls outside the task's `scope_globs`** |
| `run_command` | write-policy on any path arguments + command denylist (`curl`, `wget`, `nc`, `ssh`, `git push`, `sudo`, `podman`, …); output truncated and redacted |
| `mark_task_complete` | triggers the orchestrator's verification loop (never trusted by itself) |
| `request_spec_amendment` | freezes the run and escalates to you — approve / reject with guidance / abort, via the UI or Telegram |

What agents can never do, mechanically:

- Modify test files on a `code_change`/`fix`/`refactor` task (Layer 1–3 stack above)
- Write outside the task's declared file scope (held + logged as a scope violation)
- Read `.github/`, `openspec/`, `.env*`, or credential-shaped files (always held)
- Touch the host filesystem, host secrets, or the orchestrator's database
- Make network calls (`network = none` by default; dependencies are pre-baked into the runner image and read-only cache mounts)
- Exceed the wall-clock, turn, attempt, or dollar ceilings (orchestrator-side timers, pre-flight budget denial)
- Rewrite the spec, mark their own work complete, or merge anything — every integration and merge is audit-gated in the orchestrator process

## Waves: parallel execution

Independent tasks don't have to run sequentially:

1. **DAG planning** — a Tier-1 model proposes `depends_on` edges between the spec's tasks; the scheduler computes wave levels.
2. **Mechanical demotion** — if two tasks in the same wave declare overlapping file globs (expanded against the base-commit file tree), the later task is demoted a wave. The model's opinion is irrelevant; the glob intersection decides.
3. **Concurrent execution** — each task gets its own worktree and its own container.
4. **Serialized integration** — agents never merge concurrently. Completed tasks are merged one-by-one into the run branch, each merge gated by the orchestrator-side audit **and a full local test suite run**, so semantic breakage localizes to the exact task that caused it.
5. **Hardened conflict resolution** — a merge conflict spawns a Tier-1 conflict agent; the resolution is *always*, at every tier, re-tested against the full suite **and** given a dedicated conformance review of the resolution hunk. Cap: 2 attempts, then the task is dropped and escalated to you.

## Safety rails

- [x] Rootless containers, `--cap-drop=ALL`, `no-new-privileges`, no network, pids/memory/cpu limits
- [x] Zero host secrets in container environments
- [x] Mechanical test locking: RO snapshot mount + content-hash manifest + orchestrator-side diff audit (hooks are advisory)
- [x] Frozen specs: committed, SHA-256 anchored, `openspec/**` is a protected path
- [x] Baseline comparison: per-test status *and flake status* recorded on `main` before any agent work; pre-existing failures escalate rather than trigger fix loops; new flakiness is a regression, not noise
- [x] Disjoint wave allocation, mechanically verified
- [x] Serialized integrations with per-merge audit + suite
- [x] Conflict resolutions get full re-test + targeted conformance at every tier
- [x] Untrusted-content discipline: data/instruction framing + scope interception
- [x] Bounded everything: wall-clock, turns, attempts, dollars — checked pre-flight, not just after
- [x] Secret redaction on every path to SQLite, notifications, or the browser
- [x] Tiered merge authority; nothing defaults to unattended writes to `main`
- [x] Escalation notifications (redacted) on every failure, timeout, violation, or amendment request
- [x] Atomic recovery: `kill -9` safe; worktrees disposable; state resumes from SQLite

## Project layout

```
girder/
├── girder.toml                  # dev-time project config for girder-on-girder
├── docs/
│   ├── plan.md                  # v2.2 specification (intent)
│   └── implementation-plan.md   # engineering blueprint (canonical detail)
├── migrations/                  # ordered, hash-recorded SQL migrations (001–011)
├── container/                   # runner image (CI-parity), cache warming, parity check
├── src/girder/
│   ├── cli.py                   # daemon | pump | web | migrate | recover | gc | review
│   ├── config.py                # Settings/Secrets (pydantic), girder.toml loader
│   ├── fsm.py                   # guarded transition engine — single mutation point
│   ├── db/                      # aiosqlite engine, migrations, typed repo layer
│   ├── specs/                   # generator, validator, freeze (SHA-256), amendment
│   ├── sandbox/                 # SandboxEngine + Podman/Docker runner, local (dev-only)
│   ├── gitops/                  # worktrees, audit-gated merges, DiffAudit/TestSentinel
│   ├── guard/                   # scope guard (ALLOW/ALLOW_LOGGED/VIOLATION), redactor
│   ├── budget/                  # pre-flight estimation + reconciliation tripwire
│   ├── agent/                   # runtime turn loop, tool registry, prompts, compaction
│   ├── models/                  # ModelGateway (roles, prices, retries)
│   ├── github/                  # client, CI poller, diagnostic agent, conformance, flaky
│   ├── orchestrator/            # run engine, scheduler (sequential + DAG waves), recovery, GC
│   ├── notify/                  # Telegram/Discord, Telegram amendment inbound
│   └── api/                     # FastAPI app, routes, SSE, HTMX templates
└── tests/
    ├── unit/                    # FSM, guards, budget math, redactor golden corpus…
    ├── integration/             # sandbox escapes, worktrees, crash recovery (needs podman)
    ├── e2e/                     # SC-01…SC-17 scenario suite against a sacrificial repo
    └── fixtures/e2e-target/     # tiny pytest-covered app the scenarios run against
```

## Development

```bash
uv sync                # install (creates .venv, dev group included)
uv run pytest          # unit + integration tests (integration skips without a container runtime)
uv run ruff check .
uv run mypy
```

Test layers:

- **Unit** — FSM edges, scope verdicts, redactor golden corpus, budget math, validator, compaction, decomposer, DAG overlap. Fast, no external deps.
- **Integration** (`pytest.mark.integration`) — real sandbox escape attempts (read-only remount as guest root, host-FS reach, timeout kill), worktree lifecycle, diff audits against crafted commits, `kill -9` crash recovery. Skipped automatically when no container runtime is available.
- **E2E** (`pytest.mark.e2e`) — the SC-01…SC-17 scenario catalog mapping 1:1 to the spec's phase exit criteria. Runs the real engine code paths against `tests/fixtures/e2e-target/` with a scripted fake model gateway; opt-in live-service scenarios are environment-gated.

The codebase is strictly typed (`mypy --strict` on `src/girder`) and linted with ruff. Two conventions worth knowing: all SQL lives in `db/` and `fsm.py` — nothing else touches it — and every status mutation funnels through `fsm.transition()`.

## License

Released under the [MIT](LICENSE) license.
