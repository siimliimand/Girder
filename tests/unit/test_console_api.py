"""Unit tests for the Sprint 6 console surface (WP 6.1/6.3/6.4/6.5).

Steering queue, SSE event stream, DAG graph, spend analytics, merge queue,
tier console, history and the post-mortem explorer. No network, no git:
projects/runs are seeded directly through :mod:`girder.db.repo`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx

from girder.api.app import create_app
from girder.config import AutonomyConfig, Secrets, Settings
from girder.db import repo
from girder.db.engine import Database, default_migrations_dir
from girder.db.models import TaskType

SECRET = "ghp_" + "a" * 30
AWS_KEY = "AKIA" + "B" * 16
PAT = "github_pat_" + "c" * 25


def make_app(tmp_path: Path, settings: Settings | None = None) -> Any:
    return create_app(
        db_path=tmp_path / "console.db",
        settings=settings or Settings(),
        secrets=Secrets(),
        migrations_dir=default_migrations_dir(),
    )


def make_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=False
    )


async def make_project(db: Database, name: str = "proj") -> Any:
    return await repo.create_project(db, name, ".")


async def make_run(db: Database, pid: str, branch: str = "run/abc") -> Any:
    return await repo.create_run(db, pid, "Ship it", branch=branch, budget_cap_usd=5.0)


def parse_sse(text: str) -> list[tuple[str, str]]:
    """Split a text/event-stream body into (event, data) pairs."""
    out: list[tuple[str, str]] = []
    for block in text.split("\n\n"):
        event: str | None = None
        data: str | None = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = line[len("data: "):]
        if event is not None and data is not None:
            out.append((event, data))
    return out


# ------------------------------------------------------------------- steering


async def test_steering_pause_writes_row_and_event(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        resp = await client.post(f"/api/runs/{r.id}/steer", data={"action": "pause"})
        assert resp.status_code == 303
        rows = await db.fetchall(
            "SELECT kind, payload_json FROM steering_events WHERE run_id = ?", (r.id,)
        )
        assert [x["kind"] for x in rows] == ["pause"]
        assert json.loads(rows[0]["payload_json"]) == {}
        events = await repo.list_events_for_run(db, r.id)
        assert [e["event_type"] for e in events] == ["steering_requested"]
        assert json.loads(events[0]["payload_json"]) == {"kind": "pause"}


async def test_steering_inject_requires_directive(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        resp = await client.post(f"/api/runs/{r.id}/steer", data={"action": "inject"})
        assert resp.status_code == 400
        good = await client.post(
            f"/api/runs/{r.id}/steer", data={"action": "inject", "directive": "focus on tests"}
        )
        assert good.status_code == 303
        rows = await db.fetchall(
            "SELECT kind, payload_json FROM steering_events WHERE run_id = ?", (r.id,)
        )
        assert rows[-1]["kind"] == "inject"
        assert json.loads(rows[-1]["payload_json"]) == {"directive": "focus on tests"}


async def test_steering_skip_and_force_pass_need_run_task(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        wave = await repo.create_wave(db, r.id, 0)
        task = await repo.create_task(db, wave.id, 1, "T1", TaskType.CODE_CHANGE)

        bad = await client.post(
            f"/api/runs/{r.id}/steer", data={"action": "skip", "task_id": "nope"}
        )
        assert bad.status_code == 400
        ok_skip = await client.post(
            f"/api/runs/{r.id}/steer", data={"action": "skip", "task_id": task.id}
        )
        assert ok_skip.status_code == 303
        ok_force = await client.post(
            f"/api/runs/{r.id}/steer", data={"action": "force_pass", "task_id": task.id}
        )
        assert ok_force.status_code == 303
        rows = await db.fetchall(
            "SELECT kind, payload_json FROM steering_events WHERE run_id = ? ORDER BY id",
            (r.id,),
        )
        assert [x["kind"] for x in rows] == ["skip", "force_pass"]
        assert all(json.loads(x["payload_json"]) == {"task_id": task.id} for x in rows)


async def test_steering_unknown_action_and_abort(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        bad = await client.post(f"/api/runs/{r.id}/steer", data={"action": "explode"})
        assert bad.status_code == 400
        abort = await client.post(f"/api/runs/{r.id}/steer", data={"action": "abort"})
        assert abort.status_code == 303
        rows = await db.fetchall(
            "SELECT kind FROM steering_events WHERE run_id = ?", (r.id,)
        )
        assert [x["kind"] for x in rows] == ["abort"]


# ------------------------------------------------------------------------ SSE


async def test_sse_classes_and_redaction(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        await repo.insert_event(db, "turn_start", {"turn": 1}, run_id=r.id)
        await repo.insert_event(db, "scope_violation", {"detail": SECRET}, run_id=r.id)
        await repo.insert_event(db, "state_transition", {"to": "active"}, run_id=r.id)

        resp = await client.get(f"/api/runs/{r.id}/events?after_id=0&max_ticks=1&poll_s=0.05")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.headers["cache-control"] == "no-cache"
        events = parse_sse(resp.text)
        by_type = {json.loads(d)["event_type"]: (e, d) for e, d in events}
        assert by_type["turn_start"][0] == "tool"
        assert by_type["scope_violation"][0] == "violation"
        assert by_type["state_transition"][0] == "state"
        assert SECRET not in resp.text
        assert "***[REDACTED:" in resp.text


async def test_sse_ends_on_terminal_status(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        from tests.conftest import seed_run_status

        await seed_run_status(db, r.id, "merged")
        resp = await client.get(f"/api/runs/{r.id}/events?after_id=0&max_ticks=5&poll_s=0.05")
        pairs = parse_sse(resp.text)
        assert pairs[-1] == ("end", json.dumps({"status": "merged"}))


# ---------------------------------------------------------------------- graph


async def test_graph_structure(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        w0 = await repo.create_wave(db, r.id, 0)
        t1 = await repo.create_task(
            db, w0.id, 1, "First", TaskType.CODE_CHANGE, scope_globs=["src/**"]
        )
        t2 = await repo.create_task(
            db, w0.id, 2, "Second", TaskType.TEST_CHANGE, depends_on=[t1.id]
        )
        body = (await client.get(f"/api/runs/{r.id}/graph")).json()
        assert body["run_id"] == r.id
        assert body["paused"] is False
        assert len(body["waves"]) == 1
        wave = body["waves"][0]
        assert wave["sequence_order"] == 0
        assert [t["id"] for t in wave["tasks"]] == [t1.id, t2.id]
        assert wave["tasks"][1]["depends_on"] == [t1.id]
        assert wave["tasks"][0]["scope_globs"] == ["src/**"]
        assert wave["tasks"][0]["task_type"] == TaskType.CODE_CHANGE
        assert wave["tasks"][0]["status"] == "pending"
        assert body["unwaved_tasks"] == []


# ---------------------------------------------------------------------- spend


async def test_spend_extension_roles_and_violations(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        uid = await repo.insert_token_usage(
            db,
            run_id=r.id,
            attempt_id=None,
            model_role="builder",
            model_id="claude-x",
            estimated_before_call=0.01,
        )
        await repo.update_token_usage_actual(
            db, uid, prompt_tokens=10, completion_tokens=5, cost_usd=0.002
        )
        body = (await client.get(f"/api/runs/{r.id}/spend")).json()
        assert body["cap"] == 5.0
        assert body["spend_usd"] == 0.0
        assert body["projected_spend_usd"] == 0.0
        assert body["usage"][0]["model_id"] == "claude-x"
        assert body["violations"] == 0
        assert body["roles"] == [
            {
                "model_role": "builder",
                "model_id": "claude-x",
                "calls": 1,
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "cost_usd": 0.002,
                "estimated_usd": 0.01,
            }
        ]


# ---------------------------------------------------------------- merge queue


async def test_merge_queue_lists_only_pending_human(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p1 = await make_project(db, "alpha")
        p2 = await make_project(db, "beta")
        r1 = await make_run(db, p1.id, branch="run/one")
        r2 = await make_run(db, p2.id, branch="run/two")
        from tests.conftest import seed_run_status

        await seed_run_status(db, r1.id, "merge_pending_human")
        await seed_run_status(db, r2.id, "active")
        body = (await client.get("/api/merge-queue")).json()
        assert [q["run_id"] for q in body["queue"]] == [r1.id]
        q = body["queue"][0]
        assert q["project_id"] == p1.id
        assert q["project_name"] == "alpha"
        assert q["branch"] == "run/one"
        assert q["budget_cap_usd"] == 5.0
        assert q["spend_usd"] == 0.0
        page = await client.get("/merge-queue")
        assert page.status_code == 200
        assert "alpha" in page.text


# ----------------------------------------------------------------------- tier


async def test_tier_promotion_gating(tmp_path: Path) -> None:
    settings = Settings(autonomy=AutonomyConfig(t2_required_streak=2))
    app = make_app(tmp_path, settings)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db, "gated")

        # invalid tier string -> 400
        bad = await client.post(f"/api/projects/{p.id}/tier", data={"tier": "nine"})
        assert bad.status_code == 400
        oob = await client.post(f"/api/projects/{p.id}/tier", data={"tier": "3"})
        assert oob.status_code == 400

        # 0 -> 1 explicit opt-in
        resp = await client.post(f"/api/projects/{p.id}/tier", data={"tier": "1"})
        assert resp.status_code == 303
        assert (await repo.get_project(db, p.id)).autonomy_tier == 1

        # promotion to 2 gated on the clean-merge streak -> 409
        denied = await client.post(f"/api/projects/{p.id}/tier", data={"tier": "2"})
        assert denied.status_code == 409
        assert "streak" in denied.text

        await repo.bump_clean_merge_streak(db, p.id)
        await repo.bump_clean_merge_streak(db, p.id)
        ok = await client.post(f"/api/projects/{p.id}/tier", data={"tier": "2"})
        assert ok.status_code == 303
        assert (await repo.get_project(db, p.id)).autonomy_tier == 2

        # demotion is instant
        down = await client.post(f"/api/projects/{p.id}/tier", data={"tier": "0"})
        assert down.status_code == 303
        assert (await repo.get_project(db, p.id)).autonomy_tier == 0


async def test_tier_console_page(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db, "tiered")
        page = await client.get(f"/projects/{p.id}/tier")
        assert page.status_code == 200
        assert "T0" in page.text
        assert "T2 requires" in page.text


# -------------------------------------------------------------------- history


async def test_history_lists_runs_with_project_name(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db, "hist")
        r = await make_run(db, p.id)
        await repo.set_run_paused(db, r.id, True)
        body = (await client.get("/api/history")).json()
        assert len(body["runs"]) == 1
        row = body["runs"][0]
        assert row["run_id"] == r.id
        assert row["project_name"] == "hist"
        assert row["paused"] is True
        assert row["status"] == "draft"
        assert row["branch"] == "run/abc"
        assert row["integrity_violations"] == 0
        page = await client.get("/history")
        assert page.status_code == 200
        assert "hist" in page.text


# ----------------------------------------------------------------- post-mortem


async def test_postmortem_redacts_unredacted_rows(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        wave = await repo.create_wave(db, r.id, 0)
        task = await repo.create_task(db, wave.id, 1, "T1", TaskType.CODE_CHANGE)
        attempt = await repo.create_attempt(db, task.id, base_commit="deadbeef")
        # written directly: simulates rows stored before redaction existed
        await repo.insert_tool_call(
            db,
            attempt_id=attempt.id,
            tool_name="shell",
            input_json='{"cmd": "env"}',
            output_redacted=f"AWS_KEY={AWS_KEY}",
            duration_ms=42,
            scope_violation=True,
            held=True,
        )
        await repo.insert_event(
            db, "tool_result", {"out": f"token={PAT}"}, run_id=r.id, attempt_id=attempt.id
        )
        await repo.insert_integrity_violation(
            db, r.id, "scope_violation", {"detail": f"leak {AWS_KEY}"}, attempt_id=attempt.id
        )
        resp = await client.get(f"/runs/{r.id}/postmortem")
        assert resp.status_code == 200
        assert AWS_KEY not in resp.text
        assert PAT not in resp.text
        assert "***[REDACTED:" in resp.text
        # ordinary content still visible
        assert "shell" in resp.text
        assert "scope_violation" in resp.text


async def test_postmortem_shows_tool_and_token_rows(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        wave = await repo.create_wave(db, r.id, 0)
        task = await repo.create_task(db, wave.id, 1, "T1", TaskType.CODE_CHANGE)
        attempt = await repo.create_attempt(db, task.id, base_commit="deadbeef")
        await repo.insert_tool_call(
            db,
            attempt_id=attempt.id,
            tool_name="edit_file",
            input_json="{}",
            output_redacted="ok",
            duration_ms=7,
        )
        uid = await repo.insert_token_usage(
            db,
            run_id=r.id,
            attempt_id=attempt.id,
            model_role="builder",
            model_id="claude-x",
            estimated_before_call=0.02,
        )
        await repo.update_token_usage_actual(
            db, uid, prompt_tokens=100, completion_tokens=50, cost_usd=0.003
        )
        resp = await client.get(f"/runs/{r.id}/postmortem")
        assert resp.status_code == 200
        assert "edit_file" in resp.text
        assert "claude-x" in resp.text
        assert "builder" in resp.text
        assert "Attempt 1" in resp.text


# ------------------------------------------------------- issue 3: review window


async def test_reviewed_endpoint_inserts_event_and_clears_count(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        from tests.conftest import seed_run_status

        await seed_run_status(db, r.id, "merged")
        assert await repo.count_unreviewed_merges(db, p.id) == 1

        resp = await client.post(f"/api/runs/{r.id}/reviewed")
        assert resp.status_code == 303
        events = await repo.list_events_for_run(db, r.id)
        assert [e["event_type"] for e in events] == ["merge_reviewed"]
        assert await repo.count_unreviewed_merges(db, p.id) == 0


async def test_reviewed_endpoint_rejects_non_merged_run(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)  # spec_pending
        resp = await client.post(f"/api/runs/{r.id}/reviewed")
        assert resp.status_code == 409


async def test_tier_page_shows_review_window_and_mark_reviewed(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        from tests.conftest import seed_run_status

        await seed_run_status(db, r.id, "merged")
        resp = await client.get(f"/projects/{p.id}/tier")
        assert resp.status_code == 200
        assert "Unreviewed merges:" in resp.text
        assert "Mark reviewed" in resp.text
        # after review the button disappears and the badge flips
        await client.post(f"/api/runs/{r.id}/reviewed")
        resp2 = await client.get(f"/projects/{p.id}/tier")
        assert "Mark reviewed" not in resp2.text
        assert "reviewed" in resp2.text


# ---------------------------------------------------- issue 20: T1 run gate


async def test_t1_window_blocks_new_runs(tmp_path: Path) -> None:
    settings = Settings(autonomy=AutonomyConfig(t1_review_window=1))
    app = make_app(tmp_path, settings=settings)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        await repo.set_project_tier(db, p.id, 1)
        r = await make_run(db, p.id)
        from tests.conftest import seed_run_status

        await seed_run_status(db, r.id, "merged")  # 1 unreviewed >= window 1

        blocked = await client.post(
            f"/api/projects/{p.id}/runs", data={"intent": "another thing"}
        )
        assert blocked.status_code == 409
        assert "T1 review window exceeded" in blocked.text

        # marking it reviewed clears the window -> the run goes through
        await client.post(f"/api/runs/{r.id}/reviewed")
        ok = await client.post(
            f"/api/projects/{p.id}/runs", data={"intent": "another thing"}
        )
        assert ok.status_code == 303


async def test_project_page_surfaces_review_window_notice(tmp_path: Path) -> None:
    settings = Settings(autonomy=AutonomyConfig(t1_review_window=1))
    app = make_app(tmp_path, settings=settings)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        await repo.set_project_tier(db, p.id, 1)
        r = await make_run(db, p.id)
        from tests.conftest import seed_run_status

        await seed_run_status(db, r.id, "merged")
        resp = await client.get(f"/projects/{p.id}")
        assert "T1 review window exceeded" in resp.text


# --------------------------------------------------- issue 21: in-UI merge


class _FakeMergeOutcome:
    merged = True
    sha = "abc123"
    reason: str | None = None


class FakeGitHub:
    def __init__(self) -> None:
        self.merge_called_with: int | None = None
        self.mergeable = True

    async def pr_is_mergeable(self, pr_number: int) -> bool:
        return self.mergeable

    async def merge_pr(self, pr_number: int, **kw: Any) -> _FakeMergeOutcome:
        self.merge_called_with = pr_number
        return _FakeMergeOutcome()


async def test_merge_endpoint_merges_pending_run(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    fake = FakeGitHub()
    async with make_client(app) as client, app.router.lifespan_context(app):
        app.state.github_factory = lambda project: fake
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        from tests.conftest import seed_run_status

        await seed_run_status(db, r.id, "merge_pending_human")
        await repo.update_run_fields(db, r.id, pr_number=41)

        resp = await client.post(f"/api/runs/{r.id}/merge")
        assert resp.status_code == 303
        assert fake.merge_called_with == 41
        # no transition here — the delivery pump owns post-merge bookkeeping
        status = await db.fetchone("SELECT status FROM runs WHERE id = ?", (r.id,))
        assert status["status"] == "merge_pending_human"


async def test_merge_endpoint_rejects_non_pending_and_dirty_ledger(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    fake = FakeGitHub()
    async with make_client(app) as client, app.router.lifespan_context(app):
        app.state.github_factory = lambda project: fake
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        from tests.conftest import seed_run_status

        # wrong status
        bad = await client.post(f"/api/runs/{r.id}/merge")
        assert bad.status_code == 409

        # pending but integrity ledger dirty
        await seed_run_status(db, r.id, "merge_pending_human")
        await repo.update_run_fields(db, r.id, pr_number=7, integrity_violations=2)
        dirty = await client.post(f"/api/runs/{r.id}/merge")
        assert dirty.status_code == 409
        assert fake.merge_called_with is None


# ----------------------------------------------- issue 9-UI: prompts + verdicts


async def test_postmortem_renders_prompts_and_verdicts_redacted(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        wave = await repo.create_wave(db, r.id, 0)
        task = await repo.create_task(db, wave.id, 1, "T1", TaskType.CODE_CHANGE)
        attempt = await repo.create_attempt(db, task.id, base_commit="deadbeef")
        await repo.record_attempt_prompt(
            db, attempt.id, 0, "system", f"you are a bot, key={SECRET}", run_id=r.id
        )
        await repo.record_attempt_prompt(db, attempt.id, 1, "user", "do the thing", run_id=r.id)
        await repo.insert_tool_call(
            db,
            attempt_id=attempt.id,
            tool_name="shell",
            input_json="{}",
            output_redacted="ok",
            duration_ms=3,
            verdict="violation",
        )
        resp = await client.get(f"/runs/{r.id}/postmortem")
        assert resp.status_code == 200
        # per-turn prompt view renders, secret stays redacted
        assert "turn 0" in resp.text and "turn 1" in resp.text
        assert SECRET not in resp.text
        assert "***[REDACTED:" in resp.text
        # verdict badge renders per tool call
        assert "verdict-violation" in resp.text
        assert "violation" in resp.text


# ------------------------------------------- issue 13: impact table + estimate


_SPEC = """\
---
schema: girder.openspec/v1
title: Two tasks
intent: exercise the impact table.
tasks:
  - id: first-task
    title: Do the first thing
    type: code_change
    scope_globs: ["src/a/**"]
    success_criteria:
      - "first criterion"
  - id: second-task
    title: Do the second thing
    type: test_change
    scope_globs: ["tests/**"]
    success_criteria:
      - "second criterion"
---
Narrative.
"""


async def test_panel_renders_impact_table_and_estimate(tmp_path: Path) -> None:
    from girder.config import ModelRole, ModelsConfig

    settings = Settings(
        models=ModelsConfig(
            roles=[
                ModelRole(
                    role=role_name,
                    provider="openai",
                    model="g-x",
                    price_in_per_mtok=1.0,
                    price_out_per_mtok=2.0,
                )
                for role_name in ("tier1", "tier2", "tier3")
            ]
        )
    )
    app = make_app(tmp_path, settings=settings)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        # issue: the estimate must work with proposal_md None — first review
        # and Regenerate are exactly when the next generation call is imminent.
        # (_run_panel.html only prints the line alongside a proposal, so assert
        # on the estimator itself for the no-proposal case.)
        from girder.api.routes import _next_generation_estimate

        est_na = _next_generation_estimate(
            app.state.settings, app.state.budget, await repo.get_run(db, r.id), tmp_path
        )
        assert est_na.startswith("$")
        assert est_na != "n/a"
        await repo.update_run_fields(db, r.id, proposal_md=_SPEC)
        resp = await client.get(f"/runs/{r.id}/panel")
        assert resp.status_code == 200
        assert "first-task" in resp.text and "second-task" in resp.text
        assert "Do the first thing" in resp.text
        assert "src/a/**" in resp.text and "second criterion" in resp.text
        assert "Estimated cost of next generation call" in resp.text
        est = resp.text.split("Estimated cost of next generation call", 1)[1]
        assert "<strong>$" in est
        assert "n/a" not in est


async def test_panel_estimate_na_without_pricing(tmp_path: Path) -> None:
    app = make_app(tmp_path)  # default Settings: no model roles configured
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        await repo.update_run_fields(db, r.id, proposal_md=_SPEC)
        resp = await client.get(f"/runs/{r.id}/panel")
        assert resp.status_code == 200
        assert "first-task" in resp.text
        est = resp.text.split("Estimated cost of next generation call", 1)[1]
        assert "<strong>n/a</strong>" in est


# -------------------------------------------------------- issue 22: SSE budget


async def test_sse_budget_class_distinct_from_violation(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        await repo.insert_event(db, "budget_preflight_denied", {"est": 1}, run_id=r.id)
        await repo.insert_event(db, "scope_violation", {"detail": "x"}, run_id=r.id)
        await repo.insert_event(db, "attempt_failed", {"err": "x"}, run_id=r.id)

        resp = await client.get(f"/api/runs/{r.id}/events?after_id=0&max_ticks=1&poll_s=0.05")
        events = parse_sse(resp.text)
        by_type = {json.loads(d)["event_type"]: e for e, d in events}
        assert by_type["budget_preflight_denied"] == "budget"
        assert by_type["scope_violation"] == "violation"
        assert by_type["attempt_failed"] == "violation"


# ------------------------------------- migration 012: persisted diff streams


async def test_run_page_renders_stored_diff_redacted(tmp_path: Path) -> None:
    """§10 diff viewer: run page + post-mortem + JSON endpoint render a stored
    attempt diff, and a planted secret survives only as a redaction marker
    (historical views share one redactor with the live path)."""
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        wave = await repo.create_wave(db, r.id, 0)
        task = await repo.create_task(db, wave.id, 1, "T1", TaskType.CODE_CHANGE)
        attempt = await repo.create_attempt(db, task.id, base_commit="deadbeef")
        # caller redacts before insert (repo contract) — plant the raw secret
        # as if redaction had been skipped, to prove the render path re-redacts.
        await repo.insert_attempt_diff(
            db,
            attempt_id=attempt.id,
            task_id=task.id,
            run_id=r.id,
            base_commit="deadbeef",
            head_commit="c0ffee0",
            diff_redacted=f"diff --git a/x b/x\n+token = {SECRET}\n",
        )

        resp = await client.get(f"/runs/{r.id}")
        assert resp.status_code == 200
        assert "diff (latest attempt)" in resp.text
        assert "diff --git a/x b/x" in resp.text
        assert SECRET not in resp.text
        assert "***[REDACTED:" in resp.text

        pm = await client.get(f"/runs/{r.id}/postmortem")
        assert pm.status_code == 200
        assert "diff (base..head)" in pm.text
        assert SECRET not in pm.text

        api = await client.get(f"/api/runs/{r.id}/diffs")
        assert api.status_code == 200
        body = api.json()
        assert body["diffs"][0]["base_commit"] == "deadbeef"
        assert SECRET not in api.text


# ------------------------------------------------ §10: Amendments inbox page


async def test_amendments_inbox_empty_state(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        resp = await client.get("/amendments")
        assert resp.status_code == 200
        assert "No pending amendments." in resp.text


async def test_amendments_inbox_lists_pending_and_resolves(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    async with make_client(app) as client, app.router.lifespan_context(app):
        db: Database = app.state.db
        p = await make_project(db)
        r = await make_run(db, p.id)
        from tests.conftest import seed_run_status

        await seed_run_status(db, r.id, "awaiting_amendment")
        a = await repo.create_spec_amendment(
            db, r.id, "scope misses the helper module", "extend scope_globs to src/x/**"
        )

        resp = await client.get("/amendments")
        assert resp.status_code == 200
        assert "scope misses the helper module" in resp.text
        assert "extend scope_globs to src/x/**" in resp.text
        assert (
            f"/api/runs/{r.id}/amendments/{a.id}/approve" in resp.text
            and f"/api/runs/{r.id}/amendments/{a.id}/reject" in resp.text
            and f"/api/runs/{r.id}/amendments/{a.id}/abort" in resp.text
        )

        # Abort from the inbox bounces back to the inbox via Referer (approve
        # would need the frozen proposal file, which no seeded run has).
        act = await client.post(
            f"/api/runs/{r.id}/amendments/{a.id}/abort",
            headers={"Referer": "http://test/amendments"},
        )
        assert act.status_code == 303
        assert act.headers["location"] == "http://test/amendments"
        assert await repo.get_spec_amendment(db, a.id) is not None
        row = await db.fetchone("SELECT status FROM spec_amendments WHERE id = ?", (a.id,))
        assert row is not None and row["status"] == "aborted"
        # resolved amendments leave the inbox
        after = await client.get("/amendments")
        assert "No pending amendments." in after.text
