# WS-06 — Architecture Refactor

| | |
|---|---|
| **Source spec** | `docs/improvements-plan.md` v1.0 · Sprint 10 (WP 10.1 – WP 10.5) |
| **Status** | Merged (wave 3, 2026-09-10 — landed **out of order**, before waves 1–2, per owner-approved PM call; waves 1–2 were then integrated on top of the split layout the same day — see Coordination) |
| **Wave** | **3** — MUST start only after waves 1–2 merge (it moves the very files WS-02/03/04/05/09 touch). WS-10 lands after this. |
| **Effort** | ~1.5 weeks · ~5 new tests (mostly refactor) |
| **Owned files** | `src/girder/api/routes.py`, `src/girder/orchestrator/task_engine.py`, `src/girder/db/repo.py`, `src/girder/orchestrator/run_engine.py`, `scheduler.py`, `integrator.py`, new `src/girder/db/payloads.py` |
| **Shared files** | none during the wave — **this workstream is the serialization point of the whole plan** |

---

## Goal

Break up the three largest files to reduce maintenance friction and improve testability. **No behavior changes.**

## Current state (verified 2026-09-10)

| File | Lines | Plan estimate |
|---|---|---|
| `src/girder/api/routes.py` | 1,319 | 1,320 |
| `src/girder/db/repo.py` | 1,351 | ~1,200 |
| `src/girder/orchestrator/task_engine.py` | 973 | 974 |

## Definition of Done

- **All existing tests pass without modification.**
- `mypy --strict` clean.
- File size of any single module ≤ 600 lines.
- Old import paths keep working during the transition (shim / re-export strategy per module).

**Pre-flight:** confirm waves 1–2 are fully merged and the suite is green *before* starting. Each sub-WP lands as its own PR.

---

## WP 10.1 — Split `src/girder/api/routes.py` (1,319 lines → 7 modules)

**Current:** single file handling all HTTP routes, template rendering, SSE, and business-logic delegation.

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

1. Create `deps.py` with `get_db()`, `get_settings()`, `get_secrets()` FastAPI dependency functions, replacing per-route `request.state.*` accesses.
2. Move route handlers into their sub-module, importing from `deps` and `girder.db.repo`.
3. In `routes/__init__.py`, create one `APIRouter` per module and include all into the main app.
4. Keep the old `routes.py` as a re-export shim for one commit, then delete.

**Note:** routes added by WS-04 (clarify) and WS-05 (webhooks) move into `runs.py` / a new `webhooks.py` respectively during this split.

---

## WP 10.2 — Split `src/girder/orchestrator/task_engine.py` (973 lines → 4 modules)

**Target layout:**

```
src/girder/orchestrator/
├── task_engine.py       (≤ 300 lines — top-level execute_task, retry loop only)
├── attempt_lifecycle.py (NEW — container/worktree allocation, teardown, crash handling)
├── verification.py      (NEW — test run, DiffAudit, TestSentinel, JUnit parse)
└── work_salvage.py      (NEW — Group B salvage: dirty-worktree commit on retry)
```

**Module contracts:**

- `attempt_lifecycle.py` exports `AttemptContext` (dataclass: container_id, worktree, attempt row) and `async def allocate_attempt(...)`, `async def teardown_attempt(ctx, *, force: bool)`.
- `verification.py` exports `VerificationResult` and `async def run_verification(ctx, ...)` — runs pytest, parses JUnit XML, runs DiffAudit and TestSentinel.
- `work_salvage.py` exports `async def salvage_dirty_worktree(ctx, attempt_num) -> str | None` (returns commit SHA or None). The existing Group-B salvage logic (`_salvage_tips`) moves here.
- `task_engine.py` imports from all three and orchestrates them in the retry loop. WS-02's `_build_retry_brief` and WS-03's index hook land in their natural homes during this split.

---

## WP 10.3 — Split `src/girder/db/repo.py` (1,351 lines → 7 modules)

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

**Migration strategy:** `repo/__init__.py` does `from .projects import *; from .runs import *; ...` so all existing call sites (`from girder.db import repo; repo.create_run(...)`) work unchanged. The import path is the only refactor needed.

---

## WP 10.4 — Replace `assert` with Explicit Guards

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

Apply to all instances in: `run_engine.py`, `task_engine.py`, `scheduler.py`, `integrator.py`. (After WP 10.2, task_engine instances may live in the new submodules — sweep those too.)

---

## WP 10.5 — TypedDict Payloads for Known Event Shapes

**Why:** `dict[str, Any]` payloads lose type safety at the call site.

**Implementation:** add `src/girder/db/payloads.py`:

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

Annotate the most-called repo functions first; `mypy --strict` surfaces the remaining `Any` gaps.

---

## Internal ordering

1. WP 10.1, 10.2, 10.3 are independent of each other — land as three separate PRs in any order.
2. WP 10.4 after 10.2 (avoids touching files twice).
3. WP 10.5 anytime after 10.3.

## Coordination

- **WS-08/WS-09/WS-10** touch the route/template surface — land them before this wave, or rebase their additions into the new module layout.
- **WS-10's auth middleware** is designed against the post-split layout (`deps.py`), which is why WS-10 follows this workstream.

### Implementation record (2026-09-10, wave 3 merged out of order)

- **Ordering decision:** the "MUST start only after waves 1–2 merge" gate was waived by the owner (waves 1–2 had not been started; WS-06 is a no-behavior-change refactor with no functional dependency on them). Wave 3 landed first; wave 1 (WS-03/04/05/08/09, stacks foundation) was integrated **on top of the split layout** the same day — see commit `e63541a` ("adopt task_engine split") for the adoption pattern. Wave-1/2 docs referencing `routes.py`, `repo.py` or monolithic `task_engine.py` should now target the split modules:
  - routes: `src/girder/api/routes/` (`projects, runs, specs, steering, amendments, delivery, history, tiers, webhooks, clarify, metrics`-adjacent modules) + `api/deps.py` + `api/auth.py`; legacy `girder.api.routes` surface re-exported by `routes/__init__.py`.
  - task_engine: `task_engine.py` (execute_task + retry loop) + `attempt_lifecycle.py` + `verification.py` + `work_salvage.py`; legacy import surface re-exported.
  - repo: `src/girder/db/repo/` (16 modules) with fully re-exporting `__init__.py` — zero call-site changes (R-SP10-1 held).
- **WP 10.1:** landed as 11 route modules + `deps.py` + internal `routes/_shared.py` (shared helpers, avoids circular imports — not in the spec sketch). `app.py` untouched.
- **WP 10.2:** `task_engine.py` is 326 lines vs the ≤300 target (≤600 DoD met); residual bulk is the execute_task loop, `__init__` state and the `_container_spec` test shim. Cut lines re-derived after commit `7358bcc` moved `verify_cmd` behind the stack plugins — stack-plugin dispatch lives in `verification.py` (`VERIFY_CMD`), re-exported for `suites.py`.
- **WP 10.4:** 10 `assert x is not None` sites converted (run_engine 8, integrator 2) with entity-id-bearing messages. Left as-is: `run_engine.py:711` isinstance-narrowing assert; `db/repo/waves.py:66` (outside WP file scope). `run_engine.py` (886 lines, >600 pre-existing) is a future split candidate.
- **WP 10.5:** TypedDict fields corrected against real payload shapes: transition events key `from` (not `from_`) plus `id` and arbitrary extras (`pr_number`, `head_sha`, `merge_sha`, `fix`, `flaky`, `failed`); `BudgetEventPayload` is `total=False` (no site supplies all four spec fields). Repo side annotated as `Mapping[str, object]` params — accepts TypedDicts and legacy dicts.

## Relevant resolution log decision

- **R-SP10-1:** `repo.py` split via re-export `__init__.py` — zero call-site changes; backward-compatible migration.
