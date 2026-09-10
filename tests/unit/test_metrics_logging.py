"""WS-09: /metrics Prometheus endpoint and JsonFormatter/CLI flag tests."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from girder.api.app import create_app
from girder.config import Secrets, Settings
from girder.db.engine import Database, default_migrations_dir
from girder.logging_config import JsonFormatter


def _parse_prometheus(text: str) -> dict[str, list[float]]:
    """Minimal Prometheus text-format parser: metric name -> sample values."""
    samples: dict[str, list[float]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, _, rest = line.partition(" ")
        value = float(rest.split(" ")[-1])
        samples.setdefault(name, []).append(value)
    return samples


# --------------------------------------------------------------------- seeding


def _ts(i: int) -> str:
    return f"2026-09-10T00:00:{i:02d}Z"


async def _seed(db: Database) -> None:
    now = "2026-09-10T00:00:00Z"
    await db.execute(
        "INSERT INTO projects (id, name, repo_path, autonomy_tier, config_json,"
        " created_at, updated_at) VALUES ('p1', 'acme', '/tmp/acme', 1, '{}', ?, ?)",
        (now, now),
    )
    for run_id, status in (("r1", "merged"), ("r2", "active")):
        await db.execute(
            "INSERT INTO runs (id, project_id, intent, branch, status,"
            " budget_cap_usd, created_at, updated_at)"
            f" VALUES ('{run_id}', 'p1', 'work', 'g/{run_id}', '{status}',"
            " 5.0, ?, ?)",
            (now, now),
        )
    await db.execute(
        "INSERT INTO waves (id, run_id, sequence_order, status)"
        " VALUES ('w1', 'r1', 1, 'done')"
    )
    for task_id, task_type, status in (
        ("t1", "code_change", "completed"),
        ("t2", "fix", "failed"),
    ):
        await db.execute(
            "INSERT INTO tasks (id, wave_id, seq, title, task_type,"
            " spec_slice_md, status)"
            f" VALUES ('{task_id}', 'w1', 1, 'work', '{task_type}', 'slice',"
            f" '{status}')"
        )
    await db.execute(
        "INSERT INTO attempts (id, task_id, attempt_num, base_commit, status,"
        " turns_used, container_id, started_at, ended_at)"
        " VALUES ('a1', 't1', 1, 'abc', 'succeeded', 7, 'ctr-1', ?, ?)",
        (_ts(0), _ts(10)),
    )
    await db.execute(
        "INSERT INTO attempts (id, task_id, attempt_num, base_commit, status,"
        " turns_used) VALUES ('a2', 't2', 1, 'abc', 'failed', 3)"
    )
    await db.execute(
        "INSERT INTO attempts (id, task_id, attempt_num, base_commit, status,"
        " turns_used, container_id) VALUES ('a3', 't2', 2, 'abc', 'running', 1,"
        " 'ctr-2')"
    )
    for i, (attempt, cost) in enumerate([("a1", 0.20), ("a1", 0.05), ("a2", 0.30)]):
        await db.execute(
            "INSERT INTO token_usage (attempt_id, run_id, model_role, model_id,"
            " prompt_tokens, completion_tokens, cost_usd, estimated_before_call,"
            " created_at) VALUES (?, 'r1', 'tier1', 'm1', 10, 5, ?, ?, ?)",
            (attempt, cost, cost, _ts(i)),
        )
    await db.execute(
        "INSERT INTO integrity_violations (run_id, attempt_id, kind, detail_json, ts)"
        " VALUES ('r1', 'a1', 'out_of_scope_write', '{}', ?)",
        (_ts(1),),
    )
    # one full dispatch/completed pair (latency 5s) + one denied pre-flight
    await db.execute(
        "INSERT INTO agent_events (ts, event_type, run_id, attempt_id, payload_json)"
        " VALUES (?, 'model_call_dispatch', 'r1', 'a1', ?)",
        (_ts(0), json.dumps({"role": "tier1"})),
    )
    await db.execute(
        "INSERT INTO agent_events (ts, event_type, run_id, attempt_id, payload_json)"
        " VALUES (?, 'model_call_completed', 'r1', 'a1', ?)",
        (_ts(5), json.dumps({"role": "tier1"})),
    )
    await db.execute(
        "INSERT INTO agent_events (ts, event_type, run_id, payload_json)"
        " VALUES (?, 'budget_preflight_denied', 'r2', '{}')",
        (_ts(1),),
    )


def _open_app(db_path: Path) -> tuple[httpx.AsyncClient, Any]:
    app = create_app(
        db_path=db_path,
        settings=Settings(),
        secrets=Secrets(),
        migrations_dir=default_migrations_dir(),
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )
    lifespan = app.router.lifespan_context(app)
    return client, lifespan


async def test_metrics_valid_prometheus_on_empty_db(tmp_path: Path) -> None:
    client, lifespan = _open_app(tmp_path / "metrics.db")
    await lifespan.__aenter__()
    try:
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        assert "# HELP girder_runs_total" in resp.text
        assert "# TYPE girder_runs_total counter" in resp.text
        samples = _parse_prometheus(resp.text)  # raises on malformed lines
        assert samples["girder_active_runs"] == [0.0]
        assert samples["girder_active_containers"] == [0.0]
        assert samples["girder_budget_denials_total"] == [0.0]
    finally:
        await lifespan.__aexit__(None, None, None)
        await client.aclose()


async def test_metrics_counts_match_seeded_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "metrics.db"
    db = await Database.open(db_path, run_migrations=False)
    try:
        await db.migrate(default_migrations_dir())
        await _seed(db)
    finally:
        await db.close()

    client, lifespan = _open_app(db_path)
    await lifespan.__aenter__()
    try:
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        samples = _parse_prometheus(resp.text)

        assert samples['girder_runs_total{status="active"}'] == [1.0]
        assert samples['girder_runs_total{status="merged"}'] == [1.0]
        assert samples[
            'girder_tasks_total{status="completed",task_type="code_change"}'
        ] == [1.0]
        assert samples['girder_tasks_total{status="failed",task_type="fix"}'] == [1.0]
        assert samples['girder_attempts_total{status="succeeded"}'] == [1.0]
        assert samples['girder_attempts_total{status="failed"}'] == [1.0]
        assert samples['girder_attempts_total{status="running"}'] == [1.0]
        assert samples['girder_scope_violations_total{kind="out_of_scope_write"}'] == [1.0]
        assert samples["girder_budget_denials_total"] == [1.0]
        assert samples["girder_active_runs"] == [1.0]
        assert samples["girder_active_containers"] == [1.0]

        # histograms: finished attempts only -> turns [7, 3]; a3 is still running
        assert samples["girder_attempt_turns_count"] == [2]
        assert samples["girder_attempt_turns_sum"] == [10.0]
        assert samples['girder_attempt_turns_bucket{le="10.0"}'] == [2]
        # cost per (attempt, role): a1 = 0.25, a2 = 0.30 -> count 2, sum 0.55
        assert samples['girder_attempt_cost_usd_count{model_role="tier1"}'] == [2]
        assert samples['girder_attempt_cost_usd_sum{model_role="tier1"}'] == [
            pytest.approx(0.55)
        ]
        assert samples[
            'girder_attempt_cost_usd_bucket{le="0.5",model_role="tier1"}'
        ] == [2]
        # latency: one 5 s call; Settings() here is role-less (hermetic
        # conftest: no girder.toml), so the provider label falls back to unknown
        lat = 'girder_model_latency_seconds_'
        assert samples[lat + 'count{model_role="tier1",provider="unknown"}'] == [1]
        assert samples[lat + 'sum{model_role="tier1",provider="unknown"}'] == [5.0]
        assert samples[
            'girder_model_latency_seconds_bucket'
            '{le="10.0",model_role="tier1",provider="unknown"}'
        ] == [1]
    finally:
        await lifespan.__aexit__(None, None, None)
        await client.aclose()


# ------------------------------------------------------------- JsonFormatter


def _record(msg: str, with_exc: bool = False) -> logging.LogRecord:
    exc_info: logging._ExcInfoType = None
    if with_exc:
        try:
            raise ValueError("boom")
        except ValueError:
            exc_info = sys.exc_info()
    return logging.LogRecord(
        name="girder.test",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=("arg",),
        exc_info=exc_info,
    )


def test_json_formatter_keys_without_exc_info() -> None:
    line = JsonFormatter().format(_record("hello %s"))
    payload: dict[str, Any] = json.loads(line)
    assert set(payload) == {"ts", "level", "logger", "msg", "module"}
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "girder.test"
    assert payload["msg"] == "hello arg"


def test_json_formatter_keys_with_exc_info() -> None:
    line = JsonFormatter().format(_record("failed %s", with_exc=True))
    payload: dict[str, Any] = json.loads(line)
    assert set(payload) == {"ts", "level", "logger", "msg", "module", "exc"}
    assert "ValueError: boom" in payload["exc"]


# ----------------------------------------------------------------- CLI flag


def _run_main(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> dict[str, Any]:
    from girder import cli

    captured: dict[str, Any] = {}

    def fake_run(coro: Any) -> int:
        coro.close()  # drain the never-awaited coroutine
        return 0

    def spy_basic_config(**kwargs: Any) -> None:
        captured["kwargs"] = kwargs

    monkeypatch.setattr(cli.asyncio, "run", fake_run)
    monkeypatch.setattr(logging, "basicConfig", spy_basic_config)
    assert cli.main(argv) == 0
    return captured


def test_cli_log_format_json_wires_json_formatter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kwargs = _run_main(monkeypatch, ["daemon", "--log-format", "json"])["kwargs"]
    (handler,) = kwargs["handlers"]
    assert isinstance(handler.formatter, JsonFormatter)


def test_cli_log_format_default_is_text(monkeypatch: pytest.MonkeyPatch) -> None:
    kwargs = _run_main(monkeypatch, ["daemon"])["kwargs"]
    assert "handlers" not in kwargs
    assert kwargs["format"] == "%(asctime)s %(levelname)s %(name)s: %(message)s"
