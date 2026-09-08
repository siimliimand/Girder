# Personal AI Development Orchestrator: "Girder" — Implementation Plan

**Version:** 2.2 (Boundary-Hardened) · **Status:** Complete Engineering Specification · **Audience:** Single user / System Developer

**Changelog from v2.1:** Enforcement mechanisms moved outside the agent's control boundary; prompt-injection handling added; pre-flight budget checks replace post-hoc-only tracking; model tiers decoupled from hardcoded model names; conflict-resolution and flaky-test gates hardened; default merge gate made configurable instead of fully unattended; secret redaction added to telemetry/notification paths.

---

## 1. Executive Summary & System Philosophy

The **Personal AI Development Orchestrator** is a local-first, single-user autonomous software delivery system. You describe a desired feature or bugfix in natural language; the system produces a structured, validated specification (OpenSpec), acquires explicit user approval, and then orchestrates autonomous agents inside isolated sandboxes to implement, test, verify, and merge the code into production (`main`).

### Key Architectural Stances
1. **Vertical Slice Before Concurrency:** The end-to-end pipeline (Describe → Spec → Build → Local Test → PR → Remote CI → Merge) must be completely automated, robust, and trusted in sequential single-agent mode before any parallel wave scheduling is introduced.
2. **Mechanical Invariants Over Prompt Guidelines:** Trusting an LLM to "not edit tests" or "self-police" leads to silent failure. Test integrity, file boundaries, and budget limits are enforced via Linux filesystem permissions, worktree locks, and deterministic state machine guards.
3. **Enforcement Lives Outside the Agent's Blast Radius:** A mechanical guarantee is only real if the agent cannot undo it from inside its own sandbox. Every invariant in this document is re-stated as: *what does the agent control, and what does it not control?* Anything the agent's own shell can touch (in-guest `chmod`, a `git` hook in its own worktree, an env var) is treated as a speed bump, not a guarantee. The actual guarantee is enforced by the orchestrator process or the container/VM boundary, which the agent has no path to reach.
4. **Fail-Fast Local Verification:** GitHub CI is the final audit stamp, not the inner debugging loop. Agents run against containerized local test runners mirroring the CI environment to eliminate slow remote polling cycles.
5. **Resilience Through Ephemerality:** Worktrees, containers, and agent sessions are disposable. Orchestrator state and git commit trees are eternal. The system can be killed with `kill -9` at any microsecond and safely recover upon reboot.
6. **Trust Is Earned, Not Assumed:** A system new enough that its mechanical gates have no track record should not default to fully unattended writes to `main`. Autonomy level is a configurable, per-project dial that graduates over time, not a fixed architectural assumption.

---

## 2. Goals & Non-Goals

### 2.1 Goals (v1–v2)
- **Zero-Touch Execution Path (Opt-In):** From spec approval to merged PR on `main`, zero manual keyboard entry required unless a deterministic checkpoint fails — available once a project has graduated to full autonomy (see §2.3).
- **Strict Mechanical Sandboxing:** Agents execute inside isolated rootless containers or VMs with no access to host secrets, orchestrator state, or non-worktree filesystems, and no capability to remove the restrictions placed on them.
- **Mechanical Test Immutability:** Agents cannot weaken test suites or relax assertions on implementation tasks; code modifications and test modifications are strictly separated, and the separation is checked by a party the agent cannot influence.
- **Spec Amendment Protocol:** A formal back-channel allowing agents to request spec changes when unworkable edge cases or library constraints arise, preventing infinite retry thrashing.
- **Comprehensive Observability & Telemetry:** Full persistence of tool invocations, shell inputs/outputs, model token costs, diff streams, and decision graphs in SQLite, with secrets redacted before storage or transmission.
- **Deterministic Resource Budgets:** Hard ceilings on wall-clock time, tool turns, and cumulative token spend per attempt, task, and run, checked *before* expensive calls are dispatched, not only after they return.
- **Untrusted-Content Discipline:** Repository contents (README, source, comments, dependency metadata) read by agents are always treated as data, never as instructions, with anomalous tool-call intent flagged for review.

### 2.2 Non-Goals
- **Multi-Tenancy & SaaS Features:** No multi-user auth, team collaboration, or subscription billing.
- **Polyglot Monorepo Support:** Single-target project stacks initially (prove loop on one stack before generalizing).
- **Self-Hosted LLM Clusters:** All inference routes through OpenRouter or direct standardized vendor APIs (Anthropic/OpenAI) using tiered *capability roles*, not hardcoded model names.
- **Autonomous Architecture Redesign:** Agents solve bounded units of work; they do not autonomously rewrite system architectures without a spec-level escalation.
- **Fully Unattended Production Merges As A Default:** Full auto-merge is an earned, opt-in state per project (§2.3), not the initial default.

### 2.3 Autonomy Tiers (New)
Every project is assigned an autonomy tier, stored in project config, independently adjustable:

| Tier | Behavior |
|---|---|
| **T0 — Supervised** | Orchestrator runs the full pipeline through CI green and conformance review, then opens the PR and **stops**. Human clicks merge. This is the default for any new project. |
| **T1 — Auto-Merge, Notify** | Orchestrator merges automatically on all-green, but every merge sends a rich notification with diff summary, and the user can configure a rolling window (e.g., "pause new runs if I haven't reviewed the last 3 merges"). |
| **T2 — Full Autonomy** | Orchestrator merges automatically with no per-merge notification gate. Only available after a project has accumulated a configurable number of clean T1 merges (default: 10) with zero integrity violations. |

A project can be manually demoted a tier at any time; demotion is instant, promotion requires the earned-trust threshold above.

---

## 3. Core Concepts, State Hierarchy & Storage Model

### 3.1 Taxonomy of Terms

| Term | Definition | Scope & Lifetime |
|---|---|---|
| **Project** | An isolated directory containing a Git repository, project configuration (including autonomy tier), and `openspec/` definitions. | Persistent root |
| **Run** | An execution lifecycle tied to a single approved OpenSpec change proposal. Maps to a branch `run/<run-id>`. | Ephemeral (archived on merge) |
| **Wave** | A discrete execution phase containing one or more independent tasks. Sequential in v1; concurrent in v2. | Phase lifetime |
| **Task** | An atomic, bounded unit of work with defined file boundaries, classified as `code_change`, `test_change`, or `refactor`. | Task lifetime |
| **Attempt** | A single agent execution session trying to satisfy a task. Fails, passes, times out, or requests spec amendment. | Ephemeral session |
| **Orchestrator** | A deterministic state machine driving tasks, managing worktrees, running tests, and negotiating git operations. **The sole trust boundary for all mechanical invariants** — nothing an agent does inside its sandbox is trusted until the orchestrator independently verifies it. | System daemon |

### 3.2 Persisted State Hierarchy (SQLite)

The orchestrator's state machine is strictly backed by SQLite with write-ahead logging (`WAL=ON`) and immediate transactions:

```
Project (id, name, autonomy_tier, clean_merge_streak)
 └── Run (id, spec_hash, branch, status, budget_cap_usd, spend_usd, projected_spend_usd)
      └── Wave (id, run_id, sequence_order, status)
           └── Task (id, wave_id, type [code|test|fix], scope_globs, status, test_content_hash)
                └── Attempt (id, task_id, attempt_num, exit_code, status)
                     ├── AgentEvent (id, attempt_id, timestamp, event_type, payload)
                     ├── TokenUsage (id, attempt_id, model_role, model_id, prompt_tokens, completion_tokens, cost_usd, estimated_before_call)
                     ├── ToolCall (id, attempt_id, tool_name, input_json, output_blob_redacted, duration_ms, scope_violation BOOL)
                     └── RedactionLog (id, attempt_id, source_field, pattern_matched, timestamp)
```

Notes on new fields:
- `Project.clean_merge_streak` backs the T1→T2 promotion threshold in §2.3.
- `Run.projected_spend_usd` is written *before* each model call, from a pre-flight cost estimate (§8.3.1), so the budget tripwire can fire pre-emptively rather than only after a call already landed.
- `Task.test_content_hash` stores a hash of test file contents at task start, independent of the path-glob check, as a second detection layer for test tampering (§8.2).
- `ToolCall.scope_violation` flags any tool call that touched a path outside the task's declared scope globs, even if a later permission layer would also have blocked it — this exists to catch *intent* early and cheaply.
- `ToolCall.output_blob_redacted` and `RedactionLog` implement the secret-redaction pipeline (§8.6) — raw tool output is never persisted or transmitted unredacted.

### 3.3 Git Branching & Worktree Topography

```
main
  │
  ├── (Baseline Validation on HEAD)
  ▼
run/<run-id>  [Created off main]
  │
  ├── task/<task-id>-a  [Isolated Worktree A: /tmp/worktrees/<run-id>/<task-id>-a]
  │     └── commits -> local test verify -> orchestrator-side diff audit -> atomic fast-forward merge into run/<run-id>
  │
  ├── task/<task-id>-b  [Isolated Worktree B: /tmp/worktrees/<run-id>/<task-id>-b]
  │     └── commits -> local test verify -> orchestrator-side diff audit -> atomic fast-forward merge into run/<run-id>
  ▼
PR opened: run/<run-id> ──[Automated Review + CI]──> [T0: human merges] / [T1/T2: auto-merge per tier]
```

The "orchestrator-side diff audit" step is new: every merge into `run/<run-id>` or a wave branch is gated by a check that runs in the orchestrator's own process space against the worktree's final commit — never by a hook living inside the worktree itself.

---

## 4. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ Web Management UI (FastAPI/React or HTMX)                                  │
│ - Spec drafting, approval & diff viewer                                     │
│ - Real-time streaming log terminal (SSE / WebSocket)                        │
│ - Run steering: Pause, Resume, Abort, Force-Escalate, Inject Directive       │
│ - Spend & Token Metrics Dashboard (projected + actual)                      │
│ - Autonomy Tier control per project, merge queue for T0 projects            │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ Unix Domain Socket / REST
┌──────────────────────────────────────▼──────────────────────────────────────┐
│ Orchestrator Daemon (Deterministic Finite State Machine)                    │
│ ── The sole trust boundary. Nothing here is verifiable by the agent. ──     │
│                                                                             │
│  ┌──────────────────────┐  ┌──────────────────────┐  ┌────────────────────┐ │
│  │ Spec Engine          │  │ Wave & Task Engine   │  │ Git & Worktree Mgr │ │
│  │ (OpenSpec Validator) │  │ (Dependency Graph)   │  │ (CoW, Cleanup)     │ │
│  └──────────────────────┘  └──────────────────────┘  └────────────────────┘ │
│  ┌──────────────────────┐  ┌──────────────────────┐  ┌────────────────────┐ │
│  │ Mechanical Sentinel  │  │ Budget & Timeout     │  │ Telemetry Logger   │ │
│  │ (Out-of-guest RO     │  │ Guardrails           │  │ (SQLite WAL,       │ │
│  │  mounts + diff audit)│  │ (pre-flight + actual)│  │  redaction pass)   │ │
│  └──────────────────────┘  └──────────────────────┘  └────────────────────┘ │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │ Scope & Injection Guard (flags out-of-scope tool calls and             │ │
│  │ instruction-like content surfaced from repo files before it can act)   │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
└──────────┬───────────────────────────┬──────────────────────────┬───────────┘
           │                           │                          │
┌──────────▼───────────┐    ┌──────────▼───────────┐   ┌──────────▼───────────┐
│ Dynamic Agent Runtime│    │ GitHub Automation    │   │ Notification Engine  │
│ - Tool execution loop│    │ - PR creation        │   │ - Telegram / Discord │
│ - Token tracking     │    │ - CI status poller   │   │ - Rich escalation    │
│ - Context manager    │    │ - Fast merge handler │   │   payloads, redacted │
└──────────┬───────────┘    └──────────────────────┘   └──────────────────────┘
           │
┌──────────▼──────────────────────────────────────────────────────────────────┐
│ Execution Sandbox (Rootless Container / MicroVM)                            │
│                                                                             │
│  ┌─────────────────────────────────┐   ┌──────────────────────────────────┐ │
│  │ Target Worktree (RW)            │   │ Project Test Suite               │ │
│  │ (Specific task file paths only) │   │ (Bind-mounted RO from OUTSIDE    │ │
│  │                                  │   │  the container; CAP_SYS_ADMIN    │ │
│  │                                  │   │  dropped so it cannot remount)   │ │
│  └─────────────────────────────────┘   └──────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │ Global Package Dependency Cache (Read-Only Mount)                      │ │
│  │ (/root/.cache/pip, /root/.cargo/registry, /root/.npm)                  │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │ Zero Host Secrets / Mocked Environment Variables                       │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Guiding Principles & Invariants

1. **Specs Are Immutable Contracts:** Once the user clicks "Approve," the OpenSpec document is frozen and mounted read-only for the duration of the run. Agents cannot rewrite specs to mark work as complete.
2. **Mechanical Test Segregation, Verified Outside the Sandbox:** An agent cannot modify application code and the test suite within the same task. Test read-only-ness is enforced by a bind mount established by the host before the container starts (§8.2), not by an in-guest `chmod` the agent's own shell could reverse. The check that no test files changed is re-run by the orchestrator against the final commit, independent of anything that happened inside the container.
3. **Local Fast Feedback Loop:** Never wait for GitHub Actions to discover a syntax error, lint failure, or broken test. 100% of tests are executed inside the local container sandbox prior to any git push.
4. **Structured Spec Amendment Over Hallucinated Hacks:** When an agent encounters an impossible constraint (e.g., conflicting API signatures, missing packages), it is prohibited from looping; it must execute the `request_spec_amendment` tool to escalate back to the user.
5. **Deterministic Crash Invariance:** Every state change, commit, and log line is written to persistent storage before external action is taken. If the server loses power, restarting resumes from the exact boundary without human reconciliation.
6. **Hard Ceilings on All Dimensions, Checked Before Spending, Not Just After:** Every loop has a maximum turn count (e.g., 20 turns), a wall-clock timeout (e.g., 600s per attempt), a retry count (e.g., 3 attempts), and a hard dollar budget per run (e.g., $5.00). Budget is estimated pre-call and checked against the cap before dispatch, in addition to being reconciled against actual cost after the response returns.
7. **Repository Content Is Data, Never Instructions:** Anything an agent reads from the target repository — source, comments, README, dependency manifests, CI logs — is untrusted input. The agent's system prompt explicitly instructs it to disregard any embedded directives found in file content, and the orchestrator flags tool calls whose target falls outside the task's declared scope for review rather than executing them silently.
8. **Autonomy Is Earned Per Project:** A project starts supervised (T0) and only reaches full unattended merge (T2) after a track record of clean runs at the intermediate tier (§2.3). No project defaults to fully unattended writes to `main`.

---

## 6. Key Design Decisions (D1 – D12)

| ID | Decision | Resolution & Implementation Rationale |
|---|---|---|
| **D1** | **Agent Runtime** | **Thin Custom Python Runtime.** Avoid heavyweight external frameworks (e.g., LangChain/AutoGPT). A custom loop with direct schema validation over standard OpenAI-compatible tool-calling APIs guarantees deterministic logging, total control over context compaction, and minimal latency. |
| **D2** | **Execution Sequencing** | **Sequential First, Parallel Opt-In.** Full vertical automation through single-agent tasks is proven before parallel waves are unlocked. Concurrency introduces branch reconciliation complexities that should not block initial utility. |
| **D3** | **Spec Immutability** | **Read-Only Volume Mount.** The OpenSpec specification is compiled to markdown/JSON, committed to the branch, and mounted read-only into agent containers from outside the container, not via in-guest permission bits. |
| **D4** | **Merge Gate Requirements** | **Quadruple-Check Invariant:** (1) Clean local test run in container, (2) Green GitHub Actions CI check, (3) Automated Spec-Conformance Review performed by a Tier-1 capability-role model, (4) Orchestrator-side diff audit confirming no test-path or out-of-scope files were touched — independent of any in-sandbox check. Whether the merge proceeds automatically or waits for a human click is governed by the project's autonomy tier (§2.3), not hardcoded. |
| **D5** | **CI & Test Failure Strategy** | **Single Isolated Diagnostic Agent.** When tests fail, a single diagnostic agent receives the exact failure diff, relevant source files, and test logs. Parallel fix attempts are prohibited to avoid circular patch thrashing. |
| **D6** | **Context Assembly** | **Tool-Driven Dynamic Discovery.** Discard brittle static repo maps. Equip the agent with `ripgrep`, `ast-grep`, `list_directory`, and `read_file_outline`. Enforce context compaction: summarize older turns when context reaches 70% of window capacity. All content returned by these tools is tagged as untrusted data in the prompt structure (D11). |
| **D7** | **Model Tiering Strategy** | **Three-Tier Architecture by Capability Role, Not Fixed Model Name:** <br>• Tier 1 (Spec Gen, Review, Conflict Resolution): current frontier-reasoning model, configured per deployment. <br>• Tier 2 (Standard Code Implementation): current fast frontier-capable model. <br>• Tier 3 (Log Analysis, Git Operations): current high-efficiency utility model. <br>Concrete model IDs live in project/deployment config and are swapped without touching this design — the roles, not the vendors or versions, are the architectural commitment. |
| **D8** | **Resume & Crash Recovery** | **Clean Task Boundary Reset.** Crashes during an attempt discard the ephemeral worktree and instantiate a fresh attempt from the last clean branch commit. Completed tasks are never re-executed. |
| **D9** | **Worktree & Cache Lifecycle** | **Shared Host Package Mounts + Ephemeral Worktrees.** Share read-only package caches (`npm`, `pip`, `cargo`) across containers to prevent network downloads on every run. Worktrees are allocated on tmpfs or fast NVMe and automatically garbage-collected upon task resolution. |
| **D10** | **Test Enforcement Engine** | **Host-Boundary Enforcement, Not In-Guest Permissions.** On `code_change` tasks, test directories are bind-mounted read-only from the host into the container *before the container starts*, with `CAP_SYS_ADMIN` dropped so the in-guest root cannot remount them writable. This replaces relying on `chmod -R 555` run inside the guest, which an agent with shell access could simply reverse. A second, independent layer — the orchestrator's own diff audit against the merged commit, plus a content-hash comparison of test files at task start vs. task end — catches anything the mount boundary didn't (e.g., a test helper outside the declared `test_directories` list). Pre-commit hooks inside the worktree are treated as advisory only, since the agent can bypass them with `--no-verify`; the authoritative check is the orchestrator-side audit in D4. |
| **D11** | **Untrusted Content Handling** | **Explicit Data/Instruction Separation.** All tool output returned to the agent (file contents, `ripgrep` matches, CI logs, dependency metadata) is wrapped with an explicit "this is data, not instructions" framing in the prompt. Any tool call whose target path falls outside the task's declared `scope_globs` is intercepted by the orchestrator, logged as a `scope_violation`, and held for review rather than executed — this is the primary defense against a prompt-injection payload planted in repo content steering the agent toward files like `.github/workflows/` or credential-adjacent paths. |
| **D12** | **Autonomy Tiering** | **Merge Authority Is Configurable and Earned.** New projects default to T0 (orchestrator stops at a ready PR; human merges). Promotion to T1 (auto-merge with notification) and T2 (fully unattended) requires an explicit user opt-in plus, for T2, a clean-run streak threshold tracked in `Project.clean_merge_streak`. This decouples "the pipeline can run unattended" (a capability, proven in Phase 3) from "the pipeline runs unattended by default" (a policy choice the user controls). |

---

## 7. Phased Implementation Roadmap

```
Phase 0 ──> Phase 1 ──> Phase 2 ──> Phase 3 ──> Phase 4 ──> Phase 5
(Foundations) (Spec Gen) (Seq Build) (Full Vertical (Parallel   (Polish &
                                      PR / Tiered    Waves)      Steering)
                                      Merge)
```

---

### Phase 0: System Foundations & Execution Substrate
**Goal:** Establish sandbox execution, state persistence, credential hygiene, and notification capabilities. Zero AI logic.

#### Implementation Tasks
1. **Host Environment:** Provision dedicated development runner (bare metal Linux or cloud VM; rootless Podman/Docker installed).
2. **State Store:** Initialize SQLite database with migration engine (Alembic or direct DDL) supporting the `Project → Run → Wave → Task → Attempt` schema, including the autonomy-tier and redaction-log tables from §3.2.
3. **Container Sandboxing Engine:**
   - Configure unprivileged, network-restricted container templates.
   - Configure read-only volume binds for package managers, established from the host side:
     - `/var/cache/orchestrator/npm -> /root/.npm:ro`
     - `/var/cache/orchestrator/pip -> /root/.cache/pip:ro`
     - `/var/cache/orchestrator/cargo -> /usr/local/cargo/registry:ro`
   - Drop `CAP_SYS_ADMIN` (and other unneeded capabilities) on every container so read-only bind mounts cannot be remounted writable from inside the guest.
4. **Git Worktree Manager:**
   - Implement `WorktreeManager` class: creates `git worktree add`, provisions branch `task/<task-id>`, and implements automated cleanup (`git worktree remove --force`).
5. **Mechanical Test Guard (Host-Side):** Implement host-managed read-only bind mounts for test directories into the container (not in-guest `chmod`). Implement the independent orchestrator-side diff-and-hash audit described in D10, run against the worktree from the orchestrator process after the container exits.
6. **Secret Redaction Pipeline:** Implement a redaction pass (regex set for common credential shapes — API keys, tokens, private key headers — plus a denylist of known secret env var names) that every piece of tool output and log line passes through before it is written to SQLite or sent to a notification channel.
7. **Notification Channel:** Implement Telegram / Discord webhook client capable of sending text, error traces, and markdown diff snippets, routed through the redaction pipeline in (6).

**Exit Criteria:**
- A synthetic state machine run successfully transitions states across a simulated restart.
- An unprivileged container executes a mock shell script, cannot touch host filesystem, cannot remount its read-only test bind mount writable even as in-guest root, shares the package cache, and is terminated by a 5-second timeout.
- A test alert containing a planted fake credential string arrives in the notification channel with the credential redacted.

---

### Phase 1: Spec Generation & Approval Pipeline
**Goal:** Transform natural language input into a deterministic, validated OpenSpec proposal, approve it via UI, and freeze it.

#### Implementation Tasks
1. **Spec Generation Agent:**
   - Construct prompt template injecting: base repo README, existing architecture conventions, and target user intent, with repo-sourced content explicitly framed as untrusted data (D11).
   - Invoke the Tier 1 capability-role model via OpenRouter to produce an OpenSpec change proposal (markdown + YAML frontmatter specifying tasks, files touched, and success criteria).
2. **Deterministic Structural Validation:**
   - Execute OpenSpec CLI validation against the generated proposal in-memory.
   - Reject malformed structural schemas automatically before user review.
3. **Spec Review Interface:**
   - Web UI screen displaying the proposal, file impact table, estimated token cost, and three buttons: **Approve**, **Edit**, **Regenerate**.
4. **Spec Freezing & Git Anchor:**
   - On **Approve**: commit the proposal to `openspec/proposals/<run-id>.md` on `run/<run-id>`.
   - Calculate SHA-256 hash of the spec and record it in the SQLite `Run` record.

**Exit Criteria:**
- Real user input yields a structurally valid OpenSpec document.
- The approved document is committed to git and cannot be modified without changing its SHA-256 hash.

---

### Phase 2: Sequential Single-Agent Build Engine
**Goal:** Approved plan → local working code + passing tests via single-agent loop.

#### Implementation Tasks
1. **Baseline Pre-Flight Check:**
   - Checkout `main`, spin up the container sandbox, run the entire test suite, and record exact baseline exit codes, test failure counts, and per-test flake status in SQLite.
2. **Task Decomposer:** Parse approved OpenSpec document into an ordered list of tasks (`Task 1 -> Task 2 -> ... -> Task N`).
3. **Dynamic Context Agent Runtime (D1, D6, D11):**
   - Implement custom execution harness with specific tools:
     - `read_file(path, line_start, line_end)`
     - `write_file(path, content)`
     - `apply_patch(path, unified_diff)`
     - `find_files(glob_pattern)`
     - `ripgrep(regex_pattern, path)`
     - `run_command(cmd)` (subject to strict timeout and output truncation, output passed through the redaction pipeline before logging)
     - `request_spec_amendment(reason, proposed_patch)`
   - Every tool call is checked against the task's declared `scope_globs` before execution; out-of-scope targets are logged as `scope_violation` and held for orchestrator review rather than silently executed.
4. **Mechanical Test Shielding (D10):**
   - For `code_change` tasks, test directories are bind-mounted read-only from the host before the container starts (not `chmod` inside the guest). `Task.test_content_hash` is recorded at task start.
5. **Local Inner Verification Loop:**
   - Agent commits code incrementally.
   - Upon agent calling `mark_task_complete()`, orchestrator runs the test suite inside the container, then independently re-hashes test files and diffs the final commit against the task's declared scope from its own process — not trusting any in-container hook.
   - If tests fail: increment attempt counter, feed back failure output (redacted) to agent, retry up to 3 times.
   - If tests pass and the independent audit is clean: commit clean state to `run/<run-id>`.
   - If the independent audit finds test-path changes or scope violations despite passing tests, the attempt is marked failed and logged as an integrity violation regardless of test outcome.
6. **Pre-Flight Budget Estimation:**
   - Before dispatching each model call, estimate cost from prompt token count and a conservative max-output assumption; write `estimated_before_call` to `TokenUsage` and check against remaining `budget_cap_usd` before the call is sent. Reconcile against actual cost once the response returns.

**Exit Criteria:**
- Three distinct feature implementations succeed sequentially from spec to green local tests without manual intervention.
- An attempt that modifies tests during a `code_change` task is blocked mechanically at the host-mount level, and, as a second-layer test, an attempt that circumvents the mount (e.g., by writing a new test file outside the mounted directories) is caught by the independent hash/diff audit.
- An orchestrator crash during an attempt resumes at the start of that task upon reboot.
- A simulated pre-flight cost estimate that would exceed the remaining budget prevents a call from being dispatched at all, rather than being caught only after the call returns.

---

### Phase 3: Complete Vertical Slice — GitHub Automation & Delivery
**Goal:** Full hands-off pipeline from spec approval to a merge-ready (or, at higher autonomy tiers, merged) PR on GitHub using sequential execution.

```
Local Code Green ──> Push Branch ──> Create PR ──> Poll CI ──> Conformance Review ──> Diff Audit ──> Merge (per tier)
                                         │
                                   (CI Fails)
                                         ▼
                              Diagnostic Fix Agent (Max 3)
```

#### Implementation Tasks
1. **Local/Remote CI Parity Audit:**
   - Ensure the sandbox container image matches the GitHub Actions CI environment runner dependencies.
2. **GitHub API Integration:**
   - Implement client using scoped GitHub App or fine-grained personal access tokens, never exposed inside agent containers.
   - Automated push of branch `run/<run-id>` to remote origin.
   - Automated PR creation with template populated directly from OpenSpec metadata.
3. **CI Poller & Diagnostic Engine (D5):**
   - Poll GitHub Checks API for completion.
   - If CI checks fail:
     - Compare failure against Phase 2 baseline. If baseline was broken, ignore and escalate.
     - Compare against the project's known-flaky-test registry (see item 4) before treating any failure as novel.
     - Extract failing job logs, isolate stack traces, redact before persisting.
     - Spawn a single **Diagnostic Fix Agent** with access to failure logs and recent diffs.
     - Push fix commit, re-poll (capped at 3 attempts).
4. **Flaky Test Circuit Breaker, Baseline-Aware:**
   - If a failure is non-deterministic (re-running immediately produces green), check whether that test was already flaky on the Phase 2 baseline run for `main`.
     - If it was already flaky pre-existing on `main`: tag as flaky, proceed.
     - If it was **not** flaky on the baseline and only became flaky after the agent's diff: treat this as a real regression signal, not noise — do not silently proceed. Escalate to the diagnostic fix agent with an explicit note that the agent's change may have introduced non-determinism (e.g., a race condition), and require human review before merge regardless of tier.
5. **Spec Conformance Review Gate (D4):**
   - Tier 1 capability-role model analyzes `git diff main...run/<run-id>` against frozen OpenSpec.
   - Checks: (a) Did it complete all requirements? (b) Did it introduce undeclared changes?
   - Flags warnings in PR comments; escalates to user if deviation is catastrophic, regardless of autonomy tier.
6. **Orchestrator-Side Diff Audit (New, D4/D10):**
   - Independent of the container's own test run, the orchestrator re-verifies from its own process: no test-path files changed unless the task was `test_change`; no files outside declared scope were touched; test-content hashes match expectations for `code_change` tasks. Any failure here blocks merge at every tier and is logged as an integrity violation, distinct from an ordinary test failure.
7. **Tiered Merge & Clean-Up:**
   - **T0:** Orchestrator stops with a ready, fully-green PR; sends notification; waits for human merge click.
   - **T1:** Orchestrator auto-merges (squash or rebase) and sends a rich notification with diff summary; tracks the rolling review window configured for the project.
   - **T2:** Orchestrator auto-merges silently (still logged), available only once `Project.clean_merge_streak` has cleared the configured threshold.
   - On any tier, on merge: sync local `main` branch, archive OpenSpec document to `openspec/archive/`, remove local worktrees, increment or reset `clean_merge_streak` based on whether the run had any integrity violations.

**Exit Criteria:**
- A feature request is initiated, approved, implemented, pushed to GitHub, validated by CI, reviewed by the conformance agent, audited by the orchestrator's independent diff check, and either merged (T1/T2) or presented ready-to-merge (T0) without touching the keyboard beyond the initial description and, at T0, the merge click.
- An intentional CI failure (e.g., linter mismatch) is diagnosed and repaired by the fix agent.
- A test that is flaky only on the agent's branch (not on baseline `main`) is escalated as a regression rather than silently tagged flaky and waved through.

---

### Phase 4: Decomposition & Parallel Wave Orchestration
**Goal:** Safely speed up execution via concurrent worktrees and wave-based scheduling for non-dependent tasks.

#### Implementation Tasks
1. **Dependency Analysis & Wave Generation:**
   - Tier 1 capability-role model evaluates OpenSpec tasks and outputs a Directed Acyclic Graph (DAG) of dependencies.
   - Orchestrator computes wave levels:
     - **Wave 0:** Foundation tasks / schema updates.
     - **Wave 1:** Independent service/module implementations.
     - **Wave 2:** Integration tests and documentation.
2. **Mechanical Disjoint-Set Verification:**
   - Validate file-scope declarations: if Task A and Task B in Wave 1 declare overlapping file globs, the orchestrator **mechanically demotes** Task B to Wave 2 regardless of model claims.
3. **Concurrent Worktree Execution:**
   - Orchestrator spawns parallel agent containers, each isolated to its own worktree (`worktree/<run-id>/<task-id>`).
4. **Serialized Incremental Integration:**
   - Agents do not merge into a shared branch concurrently.
   - When Wave tasks complete, the orchestrator merges them **one by one** into `wave/<wave-id>`, running the D4/D10 orchestrator-side audit before each merge, not just at the end of the wave:
     ```
     Wave Branch ──[Audit+Merge Task A]──> Run Local Tests (Pass) ──[Audit+Merge Task B]──> Run Local Tests
     ```
   - Running the test suite after *each* merge isolates semantic breakages to the specific task that caused them.
5. **Conflict Resolution Path (Hardened):**
   - If a git conflict occurs during task merge, spawn a Tier 1 capability-role **Conflict Fix Agent** with both task diffs and base branch context.
   - Because a merge-conflict resolution is one of the riskiest unattended actions in the system — it makes two independently-reasoned-about pieces of logic interact for the first time — the resolved commit is **always**, at every autonomy tier:
     - Re-run against the full test suite (not a subset), and
     - Given a dedicated, targeted conformance pass by the Tier 1 model specifically on the conflict-resolution hunk (separate from the whole-run conformance review in D4), before it is allowed to proceed toward merge.
   - Cap resolution attempts at 2. If unresolved, or if the targeted conformance pass on the resolution flags a concern, drop the task from the wave and escalate to the user regardless of tier.

**Exit Criteria:**
- A multi-task run executes at least two tasks concurrently in distinct worktrees.
- An induced merge conflict or semantic integration failure is localized, diagnosed, and resolved (with mandatory full-suite re-test and targeted conformance review) or cleanly escalated.

---

### Phase 5: Visibility, Control & Operational Steering
**Goal:** Upgrade the operational console for deep inspection, live intervention, and cost control.

#### Implementation Tasks
1. **Real-Time Operational Dashboard:**
   - Stream live tool calls, outputs (redacted), and diffs via Server-Sent Events (SSE).
   - Display visual dependency graph with current state colors (Pending, Running, Verifying, Failed, Complete).
   - Surface `scope_violation` and integrity-violation events distinctly from ordinary test failures, so the user can tell "the agent's code was wrong" apart from "the agent tried to do something outside its boundary."
2. **Mid-Flight Steering Controls:**
   - **Pause Execution:** Suspend scheduling at current task boundary; hold containers.
   - **Inject User Directive:** Append a system-level instruction into an active agent's turn buffer, tagged distinctly from repo-sourced content so the agent's prompt structure can tell the difference between a trusted user directive and untrusted file content (reinforces D11).
   - **Skip / Force-Pass Task:** Manually transition task state.
   - **Abort & Revert:** Immediately kill containers, delete worktrees, and reset branch to `main`.
3. **Token & Spend Analytics:**
   - Real-time spend tracking per attempt, task, and run against configured limits, showing both projected (pre-flight) and actual (reconciled) figures.
   - Hard tripwire: if projected or actual run spend exceeds budget (e.g., $10.00), freeze all execution and send immediate notification — projected-spend checks catch runaway calls before they complete, not only after.
4. **Autonomy Tier Console:**
   - Per-project view of current tier, clean-merge streak, and a manual override to promote/demote.
   - T0 projects show a merge queue of PRs awaiting a human click.
5. **Historical Post-Mortem Explorer:**
   - Browse previous runs, inspecting the exact prompt, tool-call timeline, token consumption, and diff generation for every attempt, with redaction applied consistently to historical views as well as live ones.

**Exit Criteria:**
- User can pause an active run from the UI, inject a steering instruction, resume, and watch the agent adapt.
- Hitting a simulated budget cap — via either a projected pre-flight estimate or actual reconciled spend — instantly halts execution and triggers a notification.
- A project visibly accumulates clean-merge streak at T1 and becomes eligible for T2 promotion only once the configured threshold is met.

---

## 8. Cross-Cutting Engineering Systems

### 8.1 Sandboxing & Worktree Lifecycle Architecture
To prevent disk exhaustion and multi-gigabyte build directory churn:
- **Worktree Base Allocation:** Worktrees are provisioned under `/tmp/orchestrator-worktrees/<run-id>/<task-id>`.
- **Dependency Cache Mounting:** Never permit `npm install`, `pip install`, or `cargo build` to fetch dependencies over the public internet per worktree. Package directories are symlinked or mounted from a shared, host-managed read-only cache.
- **Garbage Collection Daemon:** A background thread monitors the state store. The instant a task reaches terminal status (`Merged`, `Failed`, `Aborted`), its worktree is pruned using `git worktree remove --force` and unmounted within 60 seconds.

### 8.2 Mechanical Test Protection Specification (Hardened)
To prevent agents from passing builds by deleting assertions, protection is layered so that no single layer being bypassed defeats the guarantee:

- **Task Typing:** Every task has an immutable enum: `task_type IN ('code_change', 'test_change', 'documentation')`.
- **Layer 1 — Host-Boundary Mount (authoritative):** Before the container starts, the host bind-mounts test directories read-only into the container's filesystem namespace. `CAP_SYS_ADMIN` is dropped inside the container so nothing running as the container's own root can remount the bind read-write:
  ```bash
  # Run on the HOST, before container start — not inside the guest
  mount --bind -o ro "$HOST_TEST_DIR" "$WORKTREE_PATH/$TEST_DIR"
  # container launched with --cap-drop=SYS_ADMIN (and other unneeded caps)
  ```
- **Layer 2 — Content Hash Comparison (independent, catches gaps in Layer 1's path coverage):** At task start, the orchestrator hashes every file under the project's configured `test_directories` plus any files matching a broader test-signal pattern (fixtures, conftest-style files, mock/stub helpers) and stores it as `Task.test_content_hash`. At task end, before merge, it re-hashes and compares — from its own process, never from anything the agent could have influenced.
- **Layer 3 — Orchestrator-Side Diff Audit (authoritative gate, not a hook the agent can bypass):** Before any commit is integrated into the wave or run branch, the *orchestrator process itself* — not a git hook living in the worktree — inspects the final commit:
  ```python
  # Executed by the orchestrator daemon, outside the container, against the
  # worktree's git history — the agent has no path to influence this check.
  changed = subprocess.run(
      ["git", "-C", worktree_path, "diff", "--name-only", base_commit, "HEAD"],
      capture_output=True,
      text=True,
  ).stdout.splitlines()
  violates = [f for f in changed if matches_test_pattern(f, project.config.test_directories)]
  if violates and task.task_type == "code_change":
      mark_attempt_failed(attempt, reason="integrity_violation:test_path_modified", files=violates)
  ```
- **In-worktree pre-commit hooks are advisory only.** They give the agent fast local feedback ("you're about to touch a test file"), but since an agent with shell access can run `git commit --no-verify` or edit the hook itself, they are never the thing that actually blocks a merge — Layers 1–3 are.

### 8.3 Budget & Cost Control (New/Expanded)

#### 8.3.1 Pre-Flight Cost Estimation
Post-hoc budget tracking alone has a race condition: a single expensive call (large context, long generation) can exceed the remaining budget before the orchestrator observes the completed cost. To close this:
1. Before dispatching a model call, the orchestrator computes `estimated_cost = (prompt_tokens * input_price) + (conservative_max_output_tokens * output_price)`, using the model's configured `max_tokens` for the call as the conservative output assumption.
2. This estimate is checked against `budget_cap_usd - spend_usd` (remaining budget). If the estimate would exceed remaining budget, the call is not dispatched; the attempt is frozen and escalated as a budget-exhaustion event rather than allowed to run and fail budget after the fact.
3. Once the actual response returns, `spend_usd` is updated with the reconciled real cost, and `estimated_before_call` remains in `TokenUsage` for audit/calibration purposes (to tune how conservative the estimate needs to be over time).

#### 8.3.2 Hard Ceilings (unchanged from v2.1, retained)
- Wall-clock timeout: Max 10 minutes per attempt.
- Loop turns: Max 20 turns per attempt.
- Attempt retry cap: Max 3 attempts per task before escalating.
- Run spend budget: Hard financial stop per run ($5.00 default), enforced both pre-flight (§8.3.1) and via post-hoc reconciliation.

### 8.4 Spec Amendment Protocol
When an agent determines that the OpenSpec instructions are physically incompatible with the codebase:
1. Agent invokes tool:
   ```json
   {
     "tool": "request_spec_amendment",
     "arguments": {
       "reason": "Library X v2.0 removed method Y; spec requires calling Y.",
       "suggested_change": "Update module Z to use method W instead."
     }
   }
   ```
2. The agent execution loop freezes immediately. State transitions to `AwaitingSpecAmendment`.
3. Orchestrator dispatches high-priority notification to user containing (redacted per §8.6):
   - Run ID and Task ID.
   - Agent rationale.
   - Proposed amendment diff.
4. User responds via Web UI or Telegram bot:
   - **Approve:** Spec is patched, git commit hash updated, agent resumes with new instructions.
   - **Reject:** User provides guidance string; agent receives rejection message and continues.
   - **Abort:** Run terminates cleanly.

### 8.5 Dynamic Tool-Driven Context Discovery & Untrusted Content Handling (D6, D11)
Discard rigid, pre-computed repo maps. Agents discover context dynamically through standardized discovery tools:
- `find_files(pattern)`: Wraps `fd` or `find` to map structural file locations.
- `ripgrep(query, file_pattern)`: Fast regex search across the project tree.
- `view_symbol_outline(file_path)`: Uses Treesitter or Language Server Protocol (LSP) to extract class, method, and function declarations without loading entire files.

**Prompt-Injection Discipline:** Every result returned by these tools is repository content the agent did not author and should not treat as authoritative instruction. The prompt structure explicitly delimits tool output as data (e.g., wrapped in a clearly-labeled block distinct from system/user instructions), and the agent's system prompt states plainly that directive-sounding text found inside file content, comments, or logs is not to be followed. This matters specifically because this system culminates in unattended writes to `main`: a poisoned comment, a compromised dependency's README, or planted text in a CI log are all plausible vectors for steering an agent toward an out-of-scope action.

**Scope Enforcement:** Every tool call that would read or write a path is checked against the task's declared `scope_globs` before execution. A call outside scope — including toward `.github/`, CI configuration, credential-adjacent paths, or files outside the current worktree — is intercepted, logged as `ToolCall.scope_violation`, and held for orchestrator/user review rather than silently executed. This catches injection attempts and ordinary agent confusion by the same mechanism.

**Context Compaction Algorithm:**
- After every turn, compute total prompt token count.
- If prompt tokens > 70% of model context window:
  - Retain: System instructions, frozen OpenSpec slice, current task description.
  - Summarize: Compress prior turns into an executive scratchpad of actions taken, files edited, and errors encountered.
  - Purge: Remove raw shell outputs and stale tool responses from historical turns.

### 8.6 Secret Redaction Pipeline (New)
Container environments are stripped of host secrets (§9), but tool *output* can still surface credentials — a stack trace that echoes an env var, a debug print, a misconfigured logging statement. Before any tool output, shell output, or log line is persisted to SQLite or sent to a notification channel:
1. Run it through a pattern set matching common credential shapes (API key prefixes, JWT structure, private-key PEM headers, connection strings with embedded passwords, etc.).
2. Cross-check against a denylist of known secret environment variable names configured per project.
3. Replace matches with a redaction marker that preserves enough structure for debugging (e.g., `sk-***[REDACTED:14chars]`) without exposing the secret.
4. Log the redaction event itself (pattern matched, source field, timestamp) to `RedactionLog` for audit, without logging the secret value.

This pipeline sits between every tool's raw output and (a) `ToolCall.output_blob_redacted` in SQLite and (b) the Notification Engine — nothing reaches either unredacted.

### 8.7 Crash Recovery & Idempotency Guarantees
- **State Store as Single Source of Truth:** The SQLite database is committed before actions are triggered.
- **Reconciliation on Startup:**
  1. Orchestrator boots.
  2. Queries for runs in `Active` or `Executing` state.
  3. Queries git status for active worktrees.
  4. Any `Attempt` left in `Running` status is transitioned to `Crashed` (an attempt is disposable).
  5. The orchestrator checks out a clean worktree from the last known good commit on the task branch and starts a fresh attempt (`attempt_num + 1`).

---

## 9. Comprehensive Safety Rails Checklist

- [ ] **Sandboxed Execution:** Agents execute inside rootless containers with unneeded capabilities (including `CAP_SYS_ADMIN`) dropped; host network isolated where possible.
- [ ] **No Host Secret Propagation:** Shell environments in containers contain zero host credentials (`~/.ssh`, `~/.aws`, `.env` stripped).
- [ ] **Mechanical Test Locking, Enforced From Outside the Guest:** Test directories bind-mounted read-only from the host before container start; independent content-hash comparison and orchestrator-side diff audit both re-verify from the orchestrator's own process, not from anything inside the worktree the agent could influence. In-worktree pre-commit hooks are advisory only.
- [ ] **Frozen Blueprint Invariant:** OpenSpec files locked and validated by SHA-256 hash; agent cannot write to `openspec/`.
- [ ] **Baseline Comparison:** Full test suite (including flake status) run on `main` before any agent work starts; pre-existing failures or flakiness cannot trigger agent fix loops, but *newly introduced* flakiness is treated as a regression signal, not noise.
- [ ] **Disjoint Wave Allocation:** Parallel tasks mechanically verified to have non-overlapping file scopes before wave scheduling.
- [ ] **Serialized Integrations:** Parallel wave tasks are merged into the wave branch sequentially, running full test suites and the orchestrator-side audit between each merge.
- [ ] **Hardened Conflict Resolution:** Any merge-conflict resolution triggers a mandatory full-suite re-test plus a dedicated conformance review of the resolution hunk, at every autonomy tier, before proceeding.
- [ ] **Untrusted Content Discipline:** Repo-sourced content is explicitly framed as data, not instructions, in every agent prompt; out-of-scope tool calls are intercepted and logged rather than executed.
- [ ] **Bounded Everything:**
  - Wall-clock timeout: Max 10 minutes per attempt.
  - Loop turns: Max 20 turns per attempt.
  - Attempt retry cap: Max 3 attempts per task before escalating.
  - Run spend budget: Hard financial stop per run ($5.00 default), enforced pre-flight (estimated) and post-hoc (actual).
- [ ] **Secret Redaction:** All persisted or transmitted tool output, logs, and notifications pass through the redaction pipeline before leaving the orchestrator boundary.
- [ ] **Tiered Merge Authority:** No project defaults to fully unattended merges to `main`; T2 (full autonomy) requires an earned clean-run streak at T1.
- [ ] **Escalation Notification:** Every failure, timeout, integrity violation, scope violation, or spec amendment request dispatches immediate alert to Telegram/Discord with actionable, redacted context.
- [ ] **Atomic Recovery:** Worktrees are disposable; crashes leave git tree in clean state; orchestrator resumes safely from SQLite state.

---

## 10. Risk Register & Failure Mode Matrix

| Failure Mode | Root Cause | Severity | Automated Mitigation | Escalation Path |
|---|---|---|---|---|
| **Silent Test Evisceration** | Agent removes broken assertions to force green test run. | Critical | Layered: host-boundary read-only mount (agent cannot reverse from inside guest) + independent content-hash comparison + orchestrator-side diff audit against final commit. No single layer being bypassed defeats the guarantee. | Hard stop; flags run as integrity violation. |
| **Prompt Injection via Repo Content** | Malicious or compromised file content (dependency README, comment, CI log) contains directive-like text aimed at steering the agent. | High | Explicit data/instruction framing in prompt structure; scope-glob enforcement on every tool call, intercepting anything targeting out-of-scope or credential-adjacent paths. | Logged as `scope_violation`; held for review, not executed. |
| **Circular CI Fix Loop** | Agent attempts fix, introduces new error; fix agent loops forever. | High | Strict retry counter (max 3 attempts); baseline failure comparison. | Aborts PR; notifies user with aggregate error trace. |
| **Spec-Implementation Paradox** | Spec mandates something technically impossible in stack. | High | Formal `request_spec_amendment` tool halts execution immediately. | Pushes spec diff to user for approval/rejection. |
| **Worktree Disk Bloat** | High volume of worktrees exhausts server inode/disk storage. | Medium | Shared package cache mounts + immediate post-task worktree garbage collection daemon. | Alerts if `/tmp` usage exceeds 80%. |
| **Semantic Wave Collisions** | Tasks touch separate files but break inter-module interfaces. | High | Tasks merged sequentially into wave branch; tests and orchestrator-side audit executed after *every single* merge. | Localizes breakage to single offending task; triggers isolated fix. |
| **Bad Merge-Conflict Auto-Resolution** | LLM resolves a conflict incorrectly, merging silently. | High | Mandatory full-suite re-test plus a dedicated, targeted conformance review of the resolution hunk (distinct from whole-run conformance review) at every tier. | Unresolved or flagged resolutions drop the task and escalate to user. |
| **Masked Regression via Flaky-Test Tag** | Agent's change introduces genuine non-determinism (e.g., a race), and the naive "re-run and it's green" check tags it as pre-existing flake. | Medium | Flake status compared against the Phase 2 baseline for `main`; newly-appearing flakiness is treated as a regression, not noise. | Escalates to diagnostic fix agent with explicit regression note; requires human review before merge at any tier. |
| **Context Window Exhaustion** | Agent dumps massive log files or codebases into context. | Medium | Dynamic context discovery via `ripgrep` + automatic prompt compaction when window hits 70%. | Truncates tool output at 100 lines / 4000 tokens. |
| **Orchestrator Host Crash** | Power loss, kernel panic, or host OOM killer. | High | SQLite WAL mode; attempts discarded on reboot; restart picks up at clean task branch commit. | Notifies user on reboot with recovery report. |
| **Token Budget Runaway** | Agent trapped in rapid prompt loop burning expensive API calls. | High | Pre-flight cost estimate checked against remaining budget *before* dispatch, plus post-hoc reconciled spend tracking. | Refuses to dispatch the call if pre-flight estimate would exceed budget; shuts down sandbox on any tripwire. |
| **Secret Leakage via Telemetry** | Tool output or logs echo a credential that reaches SQLite or a notification channel. | High | Redaction pipeline (pattern set + env-var denylist) applied to all tool output before persistence or transmission. | Redaction events logged for audit; raw secret never stored. |
| **Premature Full Autonomy** | A new, unproven project is granted unattended merge-to-`main` by default. | High | Projects default to T0 (supervised); T2 requires an earned clean-run streak at T1, configurable per project. | Manual demotion available instantly; promotion requires explicit opt-in plus threshold. |

---

## 11. Refined Build Sequence & Delivery Milestones

1. **Sprint 1 (Infrastructure & Sandboxing):**
   - SQLite schema + state machine core, including autonomy-tier and redaction-log tables.
   - Worktree manager + package cache volume mounts.
   - Container isolation engine with host-boundary read-only mounts and dropped capabilities (not in-guest permission bits).
   - Secret redaction pipeline.
   - Notification client (Telegram/Discord), routed through redaction.
2. **Sprint 2 (Spec Engine & Approval):**
   - OpenRouter integration with Tier 1 capability-role models (config-driven model IDs).
   - OpenSpec prompt pipeline + CLI structural validation.
   - Minimal web approval UI + spec freezing and hashing logic.
3. **Sprint 3 (Sequential Autonomous Loop):**
   - Custom dynamic agent runtime + discovery tools (`rg`, `read_file`, `write_file`), with explicit data/instruction framing and scope-glob enforcement.
   - Mechanical test enforcement: host-boundary mounts + content-hash comparison + orchestrator-side diff audit.
   - Baseline test execution (with flake status) + local test verification loop.
   - Pre-flight budget estimation wired into the model-call dispatch path.
   - Crash recovery & task boundary state machine logic.
4. **Sprint 4 (Vertical Delivery — GitHub Pipeline):**
   - GitHub API integration (branch push, PR automation).
   - Remote CI polling + redacted log extraction.
   - Single isolated diagnostic fix agent loop.
   - Baseline-aware flaky-test circuit breaker.
   - Spec conformance review agent + orchestrator-side diff audit as a merge gate.
   - Tiered merge (T0/T1/T2) implementation, defaulting new projects to T0.
5. **Sprint 5 (Concurrency & Wave Optimization):**
   - Task dependency DAG parser.
   - Disjoint file-scope mechanical validator.
   - Concurrent worktree scheduler.
   - Serialized incremental integration with per-merge audit + automated merge conflict agent, hardened with mandatory full-suite re-test and targeted conformance review.
6. **Sprint 6 (Control Surface & Telemetry):**
   - Real-time SSE streaming web console, distinguishing integrity/scope violations from ordinary failures.
   - Mid-flight steering controls (pause, resume, abort, inject note — tagged distinctly from untrusted repo content).
   - Autonomy tier console with clean-merge streak tracking and manual override.
   - Budget enforcement tripwires (pre-flight + actual) + telemetry explorer.
