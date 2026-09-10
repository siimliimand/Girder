# WS-09 — Metrics & Structured Logging

| | |
|---|---|
| **Source spec** | `docs/improvements-plan.md` v1.0 · Sprint 12 (WP 12.3, WP 12.4) |
| **Status** | Not started |
| **Wave** | **1** — can run in parallel with WS-01, WS-03, WS-04, WS-05, WS-07A, WS-08 |
| **Effort** | ~4 days · ~5 new tests |
| **Owned files** | `src/girder/api/metrics.py` (new), `src/girder/logging_config.py` (new — see naming note) |
| **Shared files** | `src/girder/api/app.py` (register `/metrics` — **deliberately not `routes.py`**, to avoid wave-1 contention with WS-04/WS-05), `src/girder/cli.py` (`--log-format` flag; additive, trivial merge with WS-10's commands) |

---

## Goal

Add the observability infrastructure needed to tune the system data-driven: a Prometheus metrics endpoint and structured JSON logging.

## Current state (verified 2026-09-10)

- The codebase uses `logging.getLogger()` throughout, defaulting to text format.
- No `/metrics` endpoint, no metrics module, no JSON log formatting, no `--log-format` flag.

## Definition of Done

- `GET /metrics` serves the Prometheus text format with the metric set below.
- Daemon mode can emit JSON logs via `--log-format json`; default stays human-readable text.
- No new third-party dependencies.
- New tests green; `mypy --strict` clean.

---

## WP 12.3 — Prometheus Metrics Endpoint

**New module:** `src/girder/api/metrics.py`
**Endpoint:** `GET /metrics` (Prometheus text format) — register on the app in `app.py`, not `routes.py`.

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

**Implementation:** pure Python string formatting — **no `prometheus_client` dependency** (the text format is ~10KB of spec). Query SQLite on each `/metrics` request with lightweight aggregation queries; keep the endpoint cheap (single-digit ms at current data volumes). Counters derive from existing tables (`runs`, `tasks`, `attempts`, `integrity_violations`, `token_usage`); gauges from live state.

> **Note:** counters computed by aggregation are "reset-safe" (they monotonically track table contents), but any pruning (WS-10's `girder prune`) will visibly reset them. Document this in the endpoint's `# HELP` text.

---

## WP 12.4 — Structured JSON Logging

**New module:** `src/girder/logging_config.py`

> **Naming note:** the source plan names this `src/girder/logging.py`. Prefer `logging_config.py` — a submodule named `logging` is legal in Python 3 but invites confusing imports; adjust if house convention differs.

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

**CLI:** add `--log-format json|text` to `girder daemon` (and globally where the logger is configured) in `src/girder/cli.py`. Default `text` for human use; `json` for daemon/service mode.

## Tests

- `/metrics` returns valid Prometheus text format (parse it in the test); counts match seeded DB rows; endpoint responds on an empty DB.
- `JsonFormatter`: record with/without `exc_info` → valid JSON with the exact keys above.
- CLI: `--log-format json` wires the formatter; default remains text.

## Coordination

- **cli.py:** WS-10 adds `prune` / `validate` subcommands and a `--api-key` flag in wave 3 — additive and in different command sections; trivial merges either direction.
- **app.py registration** keeps this workstream out of `routes.py`, so WS-04/WS-05 can proceed without contention. WS-06's route split leaves `app.py` alone.
- Metric coverage for new subsystems (clarify sessions, webhook runs) is **not** in scope here — WS-05/WS-04 can add counters to `metrics.py` after merge if wanted.
