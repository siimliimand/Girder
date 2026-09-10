# WS-04 — Intent Clarification Loop (Pre-Spec)

| | |
|---|---|
| **Source spec** | `docs/improvements-plan.md` v1.0 · Sprint 8 · WP 8.5 |
| **Status** | Not started |
| **Wave** | **1** — can run in parallel with WS-01, WS-03, WS-05, WS-07A, WS-08, WS-09 |
| **Effort** | ~4–5 days · ~10 new tests |
| **Owned files** | `src/girder/specs/generator.py` (clarification param), `src/girder/api/templates/clarify.html` (new), `migrations/015_clarification_sessions.sql` (new) |
| **Shared files** | `src/girder/api/routes.py` (adds clarify endpoints — additive, different section from WS-05's webhook routes), `src/girder/db/models.py` (new table + `RunStatus.CLARIFYING`), `src/girder/fsm.py` (one edge), `src/girder/config.py` (`[specs]` key) |

---

## Goal

Vague intents produce vague specs that agents cannot satisfy. Jules asks clarifying questions before generating a spec. An optional pre-spec clarification step improves first-attempt success rates while preserving today's zero-friction flow by default.

## Current state (verified 2026-09-10)

- `src/girder/fsm.py` holds the run state machine; `RunStatus` lives in `src/girder/db/models.py`. No `CLARIFYING` status exists.
- Migrations 001–013 exist in the repo-root `migrations/`; this workstream owns **`015_clarification_sessions.sql`** (number pre-allocated — see README).
- Spec generation lives in `src/girder/specs/generator.py`.

## Definition of Done

- With `clarify=true`, a run can park in `CLARIFYING` with 2–4 generated questions; answering them in the web UI transitions to `DRAFT` and triggers spec generation enriched with the answers.
- Default flow (`clarify` unset, `require_clarification = false`) is **byte-for-byte unchanged** — zero added friction.
- New tests green; `mypy --strict` clean.

---

## Flow

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

## Implementation

1. **API:** `POST /api/projects/{pid}/runs` gains an optional `clarify=true` query param.
2. **New run status:** `RunStatus.CLARIFYING` (pre-`DRAFT`), added via migration.
3. **FSM edge:** `CLARIFYING → DRAFT` in `src/girder/fsm.py`.
4. **New table** (migration `015_clarification_sessions.sql`):

   ```sql
   CREATE TABLE clarification_sessions (
       id TEXT PRIMARY KEY,
       run_id TEXT NOT NULL REFERENCES runs(id),
       questions_json TEXT NOT NULL,
       answers_json TEXT,
       created_at TEXT NOT NULL
   );
   ```

   (Check 001–013 for the house style — id generation, timestamp format, FK cascade.)
5. **Web UI:** `clarify.html` — shows the questions with text inputs; `POST /api/runs/{rid}/clarify` with the answers transitions the run to `DRAFT` and triggers spec generation.
6. **Spec generator:** `src/girder/specs/generator.py` accepts an optional `clarification: dict[str, str]` and appends it to the user intent in the system prompt.
7. **Config:** `[specs] require_clarification = false` (default off — preserves the current zero-friction flow).

## Tests

- `POST .../runs?clarify=true` on a vague intent → run lands in `CLARIFYING`, questions persisted.
- `POST /api/runs/{rid}/clarify` with answers → status `DRAFT`, spec generation receives the enriched intent (assert on the generator's prompt input).
- FSM rejects illegal edges out of `CLARIFYING` (anything except → `DRAFT`).
- Default path (`clarify` unset) produces no clarification session — current behavior regression test.

## Coordination

- **routes.py is the shared file** in wave 1: WS-04 adds clarify routes; WS-05 adds webhook routes. Both are additive in different sections — coordinate merge order, land either first.
- **db/models.py**: WS-03 adds a column to `tasks`; WS-04 adds a new table + enum value. Additive on both sides; separate migrations avoid collisions.
- **WS-05's** webhook-driven runs may also want clarification eventually — out of scope here; webhook runs always go straight to `DRAFT`.
