# Girder — Improvements Workstreams

This directory is the **parallel implementation set** split from [`docs/improvements-plan.md`](../improvements-plan.md) v1.0 (Sprints 7–13). Each `ws*.md` document is self-contained: it repeats every implementation detail its workstream needs, plus wave/conflict metadata the original plan didn't carry.

**WP numbering is stable.** Every `WP x.y` reference maps 1:1 back to the original plan (see [Traceability](#traceability)).

---

## How to use

1. Pick a workstream whose wave is open (below) and whose files aren't owned by an in-flight workstream (conflict matrix below).
2. Read its doc top-to-bottom — the **Pre-flight** section lists what to verify in code before writing any.
3. Implement, test, land as one PR per workstream (or per sub-WP where the doc says so).
4. Update the doc's **Status** field (`Not started → In progress → Merged`) as you go.

## Baseline facts (verified 2026-09-10)

- Sprints 1–6 + agent-effectiveness fix pass complete; **701 tests green** at `e999067`.
- Existing agent tools (9): `read_file`, `write_file`, `apply_patch`, `find_files`, `ripgrep`, `view_symbol_outline`, `run_command`, `mark_task_complete`, `request_spec_amendment`.
- Big files: `api/routes.py` 1,319 lines · `db/repo.py` 1,351 · `orchestrator/task_engine.py` 973.
- Migrations `001`–`013` exist in repo-root `migrations/`; **014–016 are pre-allocated** (below).
- Work salvage (Group B) already shipped in `task_engine.py` — WS-02 builds on it, not re-implements it.

## Wave plan

```
Wave 1  (fully parallel, ~3 wks wall-clock)
├── WS-01  agent tooling            (tools.py)
├── WS-03  codebase index           (new index/ module)
├── WS-04  clarify loop             (routes/specs/models)
├── WS-05  GitHub automation        (new webhook/slack modules)
├── WS-07A multi-stack plugins      (new stacks/ module, Dockerfiles)
├── WS-08  web console UI           (CSS/JS/templates only)
└── WS-09  metrics + JSON logging   (new modules, app.py, one CLI flag)

Wave 2  (after WS-01 merges, ~2 wks)
├── WS-02  agent intelligence       (prompts/runtime/context/task_engine)
└── WS-07B symbol outline phase 2   (tools.py via stack plugins)

Wave 3  (after waves 1–2 merge, ~2 wks)  ← serialization point
├── WS-06  architecture refactor    (splits routes/task_engine/repo)
└── WS-10  ops hardening            (auth on post-split layout; CLI cmds)

Wave 4  (after WS-05 deployed + WS-10, ~1 wk)
└── WS-11  self-hosting             (girder-on-girder, deployment docs)
```

Serial total from the original plan is ~12 weeks; the wave structure compresses wall-clock to ~8 weeks with up to 7 tracks running concurrently in wave 1.

## Migration-number pre-allocation

All migrations live in repo-root `migrations/`; latest is `013`. Numbers are assigned up front so parallel workstreams never collide:

| Number | Owner | Content |
|---|---|---|
| `014_codebase_index.sql` | WS-03 | `tasks.codebase_index_json` column |
| `015_clarification_sessions.sql` | WS-04 | `clarification_sessions` table |
| `016_prune_indexes.sql` | WS-10 | `(status, created_at)` index on `runs` |

## Conflict matrix

Rows are shared files; cells name the kind of touch. "owns" = exclusive owner for the wave.

| Shared file | WS-01 | WS-02 | WS-03 | WS-04 | WS-05 | WS-06 | WS-07 | WS-08 | WS-09 | WS-10 | WS-11 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `agent/tools.py` | **owns** | hooks registry | — | — | — | — | B rewrites outline | — | — | — | — |
| `agent/{prompts,runtime,context}.py` | — | **owns** | — | — | — | — | — | — | — | — | — |
| `orchestrator/task_engine.py` | — | **owns** (w2) | 1-line hook (w1) | — | — | splits | A: verify_cmd | — | — | — | — |
| `api/routes.py` | — | — | — | adds routes | adds routes | **splits** | — | — | — | auth middleware (post-split) | — |
| `api/app.py` | — | — | — | — | — | — | — | — | `/metrics` reg | — | — |
| `config.py` | — | `[limits]` | `[limits]` | `[specs]` | `[github]`/notify | — | stack validation | — | — | `[web]` | `[github]` flip |
| `cli.py` | — | — | — | — | — | — | — | — | `--log-format` | `prune`/`validate`/`--api-key` | — |
| `db/models.py` + migrations | — | — | 014 | 015 + RunStatus | Secrets keys | — | — | — | — | 016 | — |
| `templates/` + `static/` | — | — | — | `clarify.html` | — | — | — | **owns** | — | key header JS | — |

**Rule of thumb:** two workstreams in the same wave never own the same file; where both touch it additively (routes registration, config keys, migrations), the touches are in different sections and pre-numbered — merge order doesn't matter, but skim the diff for the paired workstream before pushing.

## Conventions (apply to every workstream)

- `mypy --strict` clean and **full test suite green** before any PR (701-test baseline and growing).
- New agent tools must pass **scope gating, redaction, and tool-call logging** through the existing pipeline — no exceptions.
- e2e **SC-01** must keep passing at every wave boundary; it's the cross-workstream regression sentinel.
- New DB work follows the existing migration style (read `001`–`013` first); never edit an applied migration.
- Commit style: short imperative subject with a component prefix, matching repo history (e.g. `Agent tools: add edit_file with line-range replacement`).
- Each workstream's PR description links back to its `ws*.md` doc and lists any coordination notes that materialized.

## Traceability

| Original plan WP | Workstream doc | Wave |
|---|---|---|
| 7.1 – 7.6 | [ws01-agent-tooling.md](ws01-agent-tooling.md) | 1 |
| 8.1 – 8.3 | [ws02-agent-intelligence.md](ws02-agent-intelligence.md) | 2 |
| 8.4 | [ws03-codebase-index.md](ws03-codebase-index.md) | 1 |
| 8.5 | [ws04-clarify-loop.md](ws04-clarify-loop.md) | 1 |
| 9.1 – 9.4 | [ws05-github-automation.md](ws05-github-automation.md) | 1 |
| 10.1 – 10.5 | [ws06-architecture-refactor.md](ws06-architecture-refactor.md) | 3 |
| 11.1 – 11.3 | [ws07-multi-stack.md](ws07-multi-stack.md) (Part A) | 1 |
| 11.4 | [ws07-multi-stack.md](ws07-multi-stack.md) (Part B) | 2 |
| 12.1, 12.2, 12.6 | [ws08-web-console.md](ws08-web-console.md) | 1 |
| 12.3, 12.4 | [ws09-metrics-logging.md](ws09-metrics-logging.md) | 1 |
| 12.5, 13.2, 13.3 | [ws10-ops-hardening.md](ws10-ops-hardening.md) | 3 |
| 13.1, 13.4 | [ws11-self-hosting.md](ws11-self-hosting.md) | 4 |

### Resolution log

The original plan's resolution log (R-SP7-1 … R-SP13-4) is preserved and each decision is repeated in the workstream doc it governs. New decisions made during implementation get appended to the relevant `ws*.md` doc's coordination section, with the original log in `improvements-plan.md` left untouched as the historical record.

### Companion docs

- `docs/plan.md` v2.2 — product spec
- `docs/implementation-plan.md` — engineering blueprint
- `docs/agent-effectiveness-plan.md` — Group A–H fix pass (largely shipped: see baseline facts)
