# WS-03 — Codebase Index (RAG Lite)

| | |
|---|---|
| **Source spec** | `docs/improvements-plan.md` v1.0 · Sprint 8 · WP 8.4 |
| **Status** | Merged (wave 1, 2026-09-10; repo/ + task_engine split ports at integration) |
| **Wave** | **1** — can run in parallel with WS-01, WS-04, WS-05, WS-07A, WS-08, WS-09 |
| **Effort** | ~4–5 days · ~8 new tests |
| **Owned files** | `src/girder/index/` (new module), `migrations/014_codebase_index.sql` (new) |
| **Shared files** | `src/girder/db/models.py` (one new column), `src/girder/orchestrator/task_engine.py` (one-line hook), `src/girder/config.py` (`[limits]` keys) |

---

## Goal

Agents spend 5–10 turns surveying the codebase before writing anything. A pre-computed index injected at task start eliminates most of this.

## Current state (verified 2026-09-10)

- No `src/girder/index/` module exists.
- Migrations live in the repo-root `migrations/` directory; **013** is the latest. This workstream owns **`014_codebase_index.sql`** (number pre-allocated — see README).

## Definition of Done

- Index built at attempt start, sliced by task scope, injected as a trusted prompt block.
- A task that would previously open with 5–10 `find_files` + `view_symbol_outline` calls starts with the index already in context.
- Config-gated (`inject_index = true` default) so it can be disabled per-project.
- New unit tests green; `mypy --strict` clean; e2e SC-01 passes.

---

## Implementation

### New module layout

```
src/girder/index/
├── __init__.py
├── indexer.py    # Builds the index from a repo path
└── inject.py     # Formats index slices for prompt injection
```

### `indexer.py`

Runs at task-start (before the agent's first turn), walking the worktree and building:

1. **File tree** — all paths with sizes, grouped by directory (depth-limited)
2. **Symbol map** — for each Python file: class names, function names, line numbers (via `ast.parse`)
3. **Import graph** — which modules import which (via `ast.parse` of import statements)

The index is stored as a JSON blob in the `tasks` table (new column `codebase_index_json`).

### Migration `014_codebase_index.sql`

```sql
ALTER TABLE tasks ADD COLUMN codebase_index_json TEXT;
```

(Adjust to the repo's migration conventions — check how 001–013 are structured before writing.)

### `inject.py`

Given the task's `scope_globs`, extracts the relevant slice of the index and formats it as a compact, token-efficient block:

```
[TRUSTED] Codebase index (relevant to your scope):

src/girder/api/
  routes.py (1320 lines) — classes: none; functions: _tier1_role, _require_run, _require_project, ...
  app.py (180 lines) — classes: none; functions: create_app, dispatch_generation, render

src/girder/db/
  repo.py (1200 lines) — functions: create_project, create_run, list_tasks_for_run, ...
  models.py (220 lines) — classes: Project, Run, Wave, Task, Attempt, RunStatus, ...
```

Injection respects `inject_index_max_tokens` — truncate by dropping whole directories, lowest-relevance first, never mid-line.

### Wiring

- **Hook:** build/inject the index at attempt start in `task_engine.py` (one-line call alongside the existing guidance assembly). This is this workstream's only `task_engine.py` touch.
- **Config keys:** `[limits] inject_index = true` (default `true`); `inject_index_max_tokens = 2000`.

### Rebuild policy

The index is **rebuilt on each attempt** (worktree state changes between attempts). Do not cache across attempts.

## Tests

- `tests/unit/test_index_indexer.py` — index a fixture tree: file tree completeness, symbol map correctness (classes/functions/line numbers), import graph edges.
- `tests/unit/test_index_inject.py` — scope-glob slicing includes only relevant directories; token-cap truncation drops whole directories; output format matches the spec block above.
- One integration assertion: attempt start with `inject_index = true` puts the block in the initial context.

## Coordination

- **WS-02** owns the rest of `task_engine.py` in wave 2 (retry briefs); the index hook is a separate one-liner — rebase is trivial if waves overlap.
- **WS-04** also adds a migration — numbering is pre-allocated (014 here, 015 there) so both can proceed in parallel.
- Symbol-map extraction is Python-only in this workstream; **WS-07** (multi-stack) generalizes symbol extraction per stack later.

## Relevant resolution log decision

- **R-SP8-4:** Codebase index is rebuilt per-attempt, not cached — worktree state changes between attempts; a stale index is worse than no index.
