# WS-10 — Ops Hardening: API Auth, Prune, Validate

| | |
|---|---|
| **Source spec** | `docs/improvements-plan.md` v1.0 · Sprint 12 WP 12.5 + Sprint 13 WP 13.2, WP 13.3 |
| **Status** | Not started |
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

## Relevant resolution log decision

- **R-SP12-5:** API key auth, not sessions/OAuth — minimal surface; full auth is an explicit non-goal in plan.md §2.2.
