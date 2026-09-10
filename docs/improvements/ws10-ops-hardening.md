# WS-10 — Ops Hardening: API Auth, Prune, Validate

| | |
|---|---|
| **Source spec** | `docs/improvements-plan.md` v1.0 · Sprint 12 WP 12.5 + Sprint 13 WP 13.2, WP 13.3 |
| **Status** | Merged (wave 3, 2026-09-10 — auth on post-split layout as planned; CLI parts pulled forward per the doc's allowance; landed out of order alongside WS-06, see Coordination) |
| **Wave** | **3** — auth middleware lands against the **post-split route layout** (after WS-06). The CLI parts (`prune`, `validate`) have no upstream dependency and may be pulled forward if cli.py merges with WS-09's `--log-format` flag are coordinated. |
| **Effort** | ~1 week · ~10 new tests |
| **Owned files** | `src/girder/cli.py` (new subcommands/flags), `src/girder/api/deps.py` or auth middleware module (post-WS-06), `migrations/016_prune_indexes.sql` (new) |
| **Shared files** | `src/girder/config.py` (`[web]` keys), `Secrets` model (`[web] api_key`), route registration (one middleware include) |

---

## Goal

Make a network-exposed Girder safe to operate: minimal API-key auth, bounded database growth, and pre-flight config validation.

## Current state (verified 2026-09-10)

- The console has **zero authentication** — anyone with network access to port 8787 can approve specs and trigger merges.
- `cli.py` (546 lines) has no `prune`, `validate`, or auth flags.
- `~/.local/share/girder/girder.db` grows unbounded: old runs accumulate events, tool_calls, and token_usage rows indefinitely.
- Migrations 001–013 exist; **016** is pre-allocated for this workstream (014 → WS-03, 015 → WS-04).

## Definition of Done

- With `[web] api_key` set, every mutating API endpoint rejects requests without the key; web UI works with the key configured; unset key = current behavior (localhost use).
- `girder prune --dry-run` prints an accurate deletion summary; real runs delete terminal-status runs older than N days with all cascading rows.
- `girder validate` catches all listed misconfigurations, exits 0/1 correctly.
- New tests green; `mypy --strict` clean.

---

## WP 12.5 — API Authentication

**Design (intentionally minimal — not OAuth, not sessions; full auth is an explicit non-goal per plan.md §2.2):**

1. New config key: `[web] api_key = ""` (empty = no auth, for localhost-only use).
2. If set, all API endpoints require `Authorization: Bearer <key>` **or** `X-API-Key: <key>`.
3. Web UI pages read the key from `localStorage` and include it in all fetch/form requests (extends WS-08's JS).
4. Key set via `girder web --api-key <key>` or env var `GIRDER_WEB__API_KEY`.
5. `secrets.toml` can hold `[web] api_key = "..."`.

**Implementation notes:**

- Land as FastAPI middleware/dependency in the post-WS-06 layout (a dependency in `api/deps.py` applied per-router, or app-level middleware that exempts `/metrics` and static assets).
- **Webhook endpoints (WS-05) authenticate via HMAC** — exempt them from API-key auth and document why.
- Compare with `secrets.compare_digest` — never `==`.

---

## WP 13.2 — Database Maintenance Commands

**New CLI command:** `girder prune [--older-than-days N] [--dry-run]`

- Deletes runs (and all cascading rows via FK) older than N days **with terminal status** (`merged`, `failed`, `aborted`).
- Defaults to 90 days.
- `--dry-run` prints a summary of what would be deleted (runs, and per-table row counts) without deleting.

**New migration `016_prune_indexes.sql`:** adds a `(status, created_at)` index on `runs` for efficient pruning queries.

---

## WP 13.3 — `girder validate` CLI Command

**New CLI command:** `girder validate [--config-path PATH]`

Checks:

1. All required fields present in `girder.toml`
2. Model roles: all three tiers present, prices > 0
3. `project.stack` is a known stack (reads `STACK_REGISTRY` once WS-07A merges; fall back to the current known-set before that)
4. Secrets file exists and is mode 0600
5. API keys are non-empty for configured providers
6. Sandbox runtime (podman/docker) is available in PATH
7. Runner image exists: `podman images -q girder-runner:<stack>`
8. `project.test_directories` all exist in the repo

Exits 0 on success, 1 on any failure. Prints human-friendly error messages, one per failed check — collect **all** failures before exiting, don't stop at the first.

---

## Tests

- Auth: endpoints reject missing/invalid keys (401/403 per house convention); valid Bearer and X-API-Key both pass; unset key preserves anonymous access; webhook route exempt.
- Prune: `--dry-run` deletes nothing and reports correctly; real run deletes only terminal-status runs past the horizon and cascades (assert child-table counts).
- Validate: one test per failure mode (bad stack, missing tier, mode-0644 secrets, missing podman, etc.) plus the all-failures-collected behavior.

## Coordination

- **WS-06** must merge first for the auth landing spot (`deps.py` / router layout).
- **WS-05** webhook exemption needs a joint review once both land.
- **WS-08** JS gains the API-key header injection — small follow-up edit there.
- **cli.py** conflicts with WS-09's `--log-format` flag are trivial (different sections); if pulled forward, coordinate the merge.

### Implementation record (2026-09-10, wave 3)

- **WP 12.5 auth:** landed as a router-level dependency (`require_api_key` in new `api/auth.py`) installed once on the aggregated console router — the `/static` mount is exempt *structurally* (not a router route) and `app.py` stayed untouched. Comparison via `secrets.compare_digest`; 401 missing / 403 invalid per existing `HTTPException` house style. Key resolution: `[web] api_key` (girder.toml / `GIRDER_WEB__API_KEY` / `girder web --api-key`) → `secrets.toml [web] api_key` fallback → empty = anonymous. Console: `base.html` wraps `fetch` (X-API-Key from `localStorage["girder_api_key"]`) and appends `?api_key=` to form actions; WS-08 to polish + document the localStorage key.
- **WS-05 joint review (both landed same day):** real webhook paths are `/api/webhooks/github` and `/api/webhooks/slack`; the exemption constant was corrected from the spec's tentative `/webhooks/` to `/api/webhooks/` — without this, configured API keys would have 403'd every inbound webhook. `/metrics` (WS-09) is mounted on the app itself and therefore doubly exempt (structurally + `PUBLIC_PATH_PREFIXES`).
- **WP 13.2 prune:** the schema has **no `ON DELETE CASCADE`** on any FK (spec assumed cascade) — child rows are deleted explicitly in dependency order inside one transaction; `steering_events`/`integrity_violations`/`notifications_log` (FK-less tables) included. Migration `016_prune_indexes.sql` per pre-allocation.
- **WP 13.3 validate:** check 3 now reads the real `STACK_REGISTRY` (`girder.stacks`, arrived mid-flight in `7358bcc`) instead of the interim hardcoded set. `load_settings()` rejects unknown stacks early (pydantic), so a bad stack surfaces as the config-load failure path — validate collects it as one failure and still runs settings-independent checks (collect-all contract preserved; regression-tested).
- **Testing:** 32 new tests across the three WPs (prune/validate 15, auth 11, incl. later additions); full suite green and `mypy --strict` clean at merge.

## Relevant resolution log decision

- **R-SP12-5:** API key auth, not sessions/OAuth — minimal surface; full auth is an explicit non-goal in plan.md §2.2.
