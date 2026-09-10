"""Prometheus metrics endpoint (improvements-plan WP 12.3, WS-09).

``GET /metrics`` serves the Prometheus text exposition format, rendered with
pure Python string formatting — no ``prometheus_client`` dependency. Every
scrape re-runs lightweight aggregation queries over the existing tables
(``runs``, ``tasks``, ``attempts``, ``integrity_violations``, ``token_usage``,
``agent_events``); gauges observe live state.

Reset-safety caveat (surfaced in the HELP text below): these "counters" are
recomputed by aggregation, so they track table contents monotonically in
normal operation — but any pruning of history (e.g. a future ``girder prune``)
will visibly reset them. Scrape-based ``rate()`` computations must tolerate
that reset.

Model-call latency is derived from the gateway's audit trail: each call
emits a ``model_call_dispatch`` event before the HTTP request and a
``model_call_completed`` event after it; latency is the timestamp difference
of each matched (run_id, attempt_id, role) pair. The provider label is
resolved from the configured model roles at scrape time (unknown roles
report ``provider="unknown"``).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter()

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_TURNS_BUCKETS = (1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0, float("inf"))
_COST_BUCKETS = (0.01, 0.05, 0.1, 0.5, 1.0, 5.0, float("inf"))
_LATENCY_BUCKETS = (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, float("inf"))

_PRUNING_NOTE = (
    " Counters are recomputed from table contents at scrape time and track"
    " monotonically in normal operation, but pruning history (girder prune)"
    " visibly resets them."
)


def _escape(value: str) -> str:
    """Escape a label value per the Prometheus text format."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(pairs: dict[str, str]) -> str:
    if not pairs:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(pairs.items()))
    return "{" + inner + "}"


def _histogram(
    name: str,
    help_text: str,
    samples: Iterator[tuple[dict[str, str], float]],
    buckets: tuple[float, ...],
) -> Iterator[str]:
    """Render one histogram family from per-series (labels, value) samples.

    Prometheus histograms are cumulative: each ``_bucket`` line counts
    observations <= le, and ``_sum``/``_count`` close the family.
    """
    yield f"# HELP {name} {help_text}{_PRUNING_NOTE}"
    yield f"# TYPE {name} histogram"
    series: dict[tuple[tuple[str, str], ...], list[float]] = {}
    for labels, value in samples:
        key = tuple(sorted(labels.items()))
        series.setdefault(key, []).append(value)
    for key, values in sorted(series.items()):
        labels = dict(key)
        for bound in buckets:
            cumulative = sum(1 for v in values if v <= bound)
            le = "+Inf" if bound == float("inf") else repr(bound)
            yield f"{name}_bucket{_labels({**labels, 'le': le})} {cumulative}"
        yield f"{name}_sum{_labels(labels)} {sum(values)!r}"
        yield f"{name}_count{_labels(labels)} {len(values)}"


def _counter(name: str, help_text: str,
             samples: Iterator[tuple[dict[str, str], float]]) -> Iterator[str]:
    yield f"# HELP {name} {help_text}{_PRUNING_NOTE}"
    yield f"# TYPE {name} counter"
    for labels, value in samples:
        yield f"{name}{_labels(labels)} {value!r}"


def _gauge(name: str, help_text: str,
           samples: Iterator[tuple[dict[str, str], float]]) -> Iterator[str]:
    yield f"# HELP {name} {help_text}"
    yield f"# TYPE {name} gauge"
    for labels, value in samples:
        yield f"{name}{_labels(labels)} {value!r}"


def _pairs(rows: list[Any], label_keys: tuple[str, ...], value_key: str
           ) -> Iterator[tuple[dict[str, str], float]]:
    for row in rows:
        d = dict(row)
        yield {k: str(d[k]) for k in label_keys}, float(d[value_key])


def _role_provider_map(settings: Any) -> dict[str, str]:
    """role -> provider from configured model roles (D7); empty when role-less."""
    try:
        roles = settings.models.roles
    except AttributeError:
        return {}
    return {r.role: r.provider for r in roles}


def _parse_ts(ts: str) -> float | None:
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return time.mktime(time.strptime(ts, fmt)) - time.timezone
        except ValueError:
            continue
    return None


async def _model_call_latencies(
    db: Any, role_provider: dict[str, str]
) -> AsyncIterator[tuple[dict[str, str], float]]:
    """Pair dispatch/completed audit events into per-call latency samples.

    Events are paired FIFO per (run_id, attempt_id, role): each dispatch opens
    a pending call, each completion closes the oldest pending one. Rows arrive
    ordered by id, which is insertion order.
    """
    rows = await db.fetchall(
        "SELECT event_type, ts, run_id, attempt_id, payload_json FROM agent_events"
        " WHERE event_type IN ('model_call_dispatch', 'model_call_completed')"
        " ORDER BY id"
    )
    pending: dict[tuple[str, str, str], list[float]] = {}
    for row in rows:
        d = dict(row)
        payload = json.loads(str(d["payload_json"]))
        role = str(payload.get("role", ""))
        key = (str(d["run_id"]), str(d["attempt_id"]), role)
        ts = _parse_ts(str(d["ts"]))
        if ts is None:
            continue
        if d["event_type"] == "model_call_dispatch":
            pending.setdefault(key, []).append(ts)
        else:
            stack = pending.get(key)
            if stack:
                yield ({"model_role": role,
                        "provider": role_provider.get(role, "unknown")},
                       ts - stack.pop(0))


async def _render(db: Any, settings: Any) -> str:
    role_provider = _role_provider_map(settings)

    async def q(sql: str) -> list[Any]:
        rows: list[Any] = list(await db.fetchall(sql))
        return rows

    runs_by_status = await q(
        "SELECT status, COUNT(*) AS n FROM runs GROUP BY status")
    tasks_by_status = await q(
        "SELECT status, task_type, COUNT(*) AS n FROM tasks"
        " GROUP BY status, task_type")
    attempts_by_status = await q(
        "SELECT status, COUNT(*) AS n FROM attempts GROUP BY status")
    turns = await q(
        "SELECT turns_used FROM attempts WHERE status IN"
        " ('succeeded', 'failed', 'timeout', 'crashed', 'budget_frozen',"
        "  'integrity_violation')")
    cost_per_attempt = await q(
        "SELECT attempt_id, model_role, SUM(cost_usd) AS cost FROM token_usage"
        " WHERE attempt_id IS NOT NULL GROUP BY attempt_id, model_role")
    violations_by_kind = await q(
        "SELECT kind, COUNT(*) AS n FROM integrity_violations GROUP BY kind")
    budget_denials = await q(
        "SELECT COUNT(*) AS n FROM agent_events"
        " WHERE event_type = 'budget_preflight_denied'")
    active_runs = await q(
        "SELECT COUNT(*) AS n FROM runs WHERE status IN"
        " ('baseline_running', 'active', 'ci_running', 'ci_fixing',"
        "  'conformance_review')")
    active_containers = await q(
        "SELECT COUNT(*) AS n FROM attempts"
        " WHERE status = 'running' AND container_id IS NOT NULL")

    lines: list[str] = []
    lines += _counter(
        "girder_runs_total",
        "Runs by status (all run states; terminal outcomes are merged, failed,"
        " aborted, budget_exhausted, escalated).",
        _pairs(runs_by_status, ("status",), "n"),
    )
    lines += _counter(
        "girder_tasks_total",
        "Tasks by outcome and task type.",
        _pairs(tasks_by_status, ("status", "task_type"), "n"),
    )
    lines += _counter(
        "girder_attempts_total",
        "Attempts by outcome.",
        _pairs(attempts_by_status, ("status",), "n"),
    )
    lines += _histogram(
        "girder_attempt_turns",
        "Agent turns used per finished attempt.",
        iter([({}, float(dict(r)["turns_used"])) for r in turns]),
        _TURNS_BUCKETS,
    )
    lines += _histogram(
        "girder_attempt_cost_usd",
        "Model cost in USD per attempt, per model role.",
        _pairs(cost_per_attempt, ("model_role",), "cost"),
        _COST_BUCKETS,
    )
    lines += _counter(
        "girder_scope_violations_total",
        "Integrity/scope violations by kind (test_path_modified,"
        " content_hash_mismatch, out_of_scope_write, protected_read,"
        " scope_violation).",
        _pairs(violations_by_kind, ("kind",), "n"),
    )
    latencies = [sample async for sample in _model_call_latencies(db, role_provider)]
    lines += _histogram(
        "girder_model_latency_seconds",
        "Model call round-trip latency in seconds, derived from"
        " model_call_dispatch/model_call_completed audit events.",
        iter(latencies),
        _LATENCY_BUCKETS,
    )
    lines += _counter(
        "girder_budget_denials_total",
        "Pre-flight budget denials (gateway refused to dispatch a call).",
        iter([({}, float(budget_denials[0]["n"]))]),
    )
    lines += _gauge(
        "girder_active_runs",
        "Runs currently in an active pipeline state.",
        iter([({}, float(active_runs[0]["n"]))]),
    )
    lines += _gauge(
        "girder_active_containers",
        "Sandbox containers attached to attempts currently running.",
        iter([({}, float(active_containers[0]["n"]))]),
    )
    return "\n".join(lines) + "\n"


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    """Prometheus text exposition of Girder's aggregate counters and gauges."""
    body = await _render(request.app.state.db, request.app.state.settings)
    return Response(content=body, media_type=_CONTENT_TYPE)
