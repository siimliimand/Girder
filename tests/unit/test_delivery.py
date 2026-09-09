"""Unit tests for the Sprint 4 delivery pump (WP 4.5/4.6, SC-09..SC-13 core).

Real temp git repo with a LOCAL BARE remote (real push/fetch, no network);
GitHub REST API over httpx.MockTransport; scripted model + suite sandboxes —
same idioms as test_task_engine.py, one level up the stack.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from girder.config import (
    GithubConfig,
    LimitsConfig,
    ProjectConfig,
    SandboxNetwork,
    Secrets,
    Settings,
)
from girder.db import repo
from girder.db.engine import Database
from girder.db.models import Project, Run, RunStatus, TaskType
from girder.github.client import GitHubClient
from girder.github.delivery import DeliveryEngine
from girder.guard.redact import Redactor
from girder.util import run_host_cmd
from tests.conftest import seed_run_status
from tests.unit.test_task_engine import (
    GREEN_XML,
    RED_XML,
    FakeGateway,
    FakeNotifier,
    ScriptSandbox,
    _resp,
    _tc,
)

OWNER = "acme"
NAME = "widget"
STOP = {"merged", "failed", "escalated", "merge_pending_human", "budget_exhausted"}

GOOD_VERDICT = json.dumps(
    {
        "requirements_complete": True,
        "undeclared_changes": [],
        "severity": "none",
        "summary": "all requirements implemented",
    }
)
CATASTROPHIC_VERDICT = json.dumps(
    {
        "requirements_complete": False,
        "undeclared_changes": [".github/workflows/ci.yml rewritten"],
        "severity": "catastrophic",
        "summary": "agent rewrote CI to self-approve",
    }
)


def check(name: str, conclusion: str, *, summary: str = "", text: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "html_url": f"https://github.com/{OWNER}/{NAME}/runs/1",
        "output": {"summary": summary, "text": text},
    }


class FakeGitHub(httpx.MockTransport):
    """The GitHub REST surface the delivery pump touches, in-process."""

    def __init__(self, checks: list[dict[str, Any]], *, pr_number: int = 7,
                 pr_merged: bool = False) -> None:
        super().__init__(self._handle)
        self.checks = checks
        self.pr_number = pr_number
        self.pr_merged = pr_merged
        self.api_calls: list[tuple[str, str]] = []
        self.comments: list[dict[str, Any]] = []
        self.merge_calls = 0
        self.merge_refused = False

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.api_calls.append((method, path))
        base = f"/repos/{OWNER}/{NAME}"
        if method == "GET" and path.startswith(f"{base}/commits/") and path.endswith(
            "/check-runs"
        ):
            return httpx.Response(
                200, json={"total_count": len(self.checks), "check_runs": self.checks}
            )
        if method == "GET" and path == f"{base}/pulls":
            return httpx.Response(200, json=[])
        if method == "POST" and path == f"{base}/pulls":
            return httpx.Response(201, json={"number": self.pr_number})
        if method == "GET" and path == f"{base}/pulls/{self.pr_number}":
            return httpx.Response(
                200,
                json={
                    "number": self.pr_number,
                    "state": "closed" if self.pr_merged else "open",
                    "merged": self.pr_merged,
                    "mergeable": True,
                    "merge_commit_sha": "f00dface" if self.pr_merged else None,
                },
            )
        if method == "PUT" and path == f"{base}/pulls/{self.pr_number}/merge":
            self.merge_calls += 1
            if self.merge_refused:
                return httpx.Response(405, json={"message": "pull request is not mergeable"})
            return httpx.Response(200, json={"merged": True, "sha": "f00dface"})
        if method == "POST" and "/comments" in path:
            self.comments.append(json.loads(request.content))
            return httpx.Response(201, json={})
        return httpx.Response(404, json={"message": f"unhandled {method} {path}"})


async def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = await run_host_cmd(["git", "-C", str(cwd), *args], check=check, timeout_s=30)
    return result.stdout


@dataclass
class DHarness:
    db: Database
    project: Project
    run: Run
    repo_path: Path
    sandbox: ScriptSandbox
    notifier: FakeNotifier
    settings: Settings
    api: FakeGitHub
    github: GitHubClient

    def delivery(self, gateway: FakeGateway) -> DeliveryEngine:
        return DeliveryEngine(
            db=self.db,
            settings=self.settings,
            secrets=Secrets(),
            gateway=gateway,  # type: ignore[arg-type]
            sandbox=self.sandbox,
            notifier=self.notifier,  # type: ignore[arg-type]
            redactor=Redactor(),
            github=self.github,
            repo_path=self.repo_path,
        )


@pytest.fixture
async def dh(
    db: Database, tmp_path: Path, request: pytest.FixtureRequest
) -> AsyncIterator[DHarness]:
    """Repo + bare origin + run branch + ACTIVE run. Params: tier=int."""
    tier = int(getattr(request, "param", 0))
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    await _git(repo_path, "init", "-b", "main")
    await _git(repo_path, "config", "user.email", "test@girder.local")
    await _git(repo_path, "config", "user.name", "girder-test")
    (repo_path / "src").mkdir()
    (repo_path / "src" / "lib.py").write_text("x = 1\n")
    (repo_path / "tests").mkdir()
    (repo_path / "tests" / "test_p.py").write_text("def test_ok():\n    assert True\n")
    await _git(repo_path, "add", "-A")
    await _git(repo_path, "commit", "-m", "initial")
    # local bare remote: real push/fetch, zero network
    origin = tmp_path / "origin.git"
    await run_host_cmd(["git", "init", "--bare", "-b", "main", str(origin)], timeout_s=30)
    await _git(repo_path, "remote", "add", "origin", str(origin))
    await _git(repo_path, "push", "origin", "main")

    project = await repo.create_project(db, "p", str(repo_path), autonomy_tier=tier)
    fresh = await _new_run(db, repo_path, project, "run/dlv1", "add the thing")

    settings = Settings(
        project=ProjectConfig(test_directories=["tests"]),
        limits=LimitsConfig(task_max_attempts=2, attempt_max_turns=6, attempt_wallclock_s=60,
                            ci_fix_attempts=2),
        sandbox=SandboxNetwork(),
        github=GithubConfig(poll_interval_s=0.0),
    )
    api = FakeGitHub([check("ci", "success")])
    github = GitHubClient(
        settings,
        Secrets(github_token="ghp_deliveryunittesttoken000"),
        Redactor(),
        db,
        repo_path=repo_path,
        transport=api,
        owner_repo=(OWNER, NAME),  # remote is a local bare repo, not a GitHub URL
    )
    sandbox = ScriptSandbox(suite_results=[], suite_xml=GREEN_XML)
    yield DHarness(
        db=db,
        project=project,
        run=fresh,
        repo_path=repo_path,
        sandbox=sandbox,
        notifier=FakeNotifier(),
        settings=settings,
        api=api,
        github=github,
    )
    await github.aclose()


async def _new_run(
    db: Database, repo_path: Path, project: Project, branch: str, intent: str
) -> Run:
    """Run + branch + frozen proposal + one work commit on the branch (what a
    completed run looks like pre-delivery); ``main`` stays at its old tip."""
    base = (await _git(repo_path, "rev-parse", "HEAD")).strip()
    run = await repo.create_run(db, project.id, intent, branch, 5.0)
    await seed_run_status(db, run.id, RunStatus.ACTIVE.value)
    fresh = await repo.get_run(db, run.id)
    assert fresh is not None
    await _git(repo_path, "branch", fresh.branch)
    (repo_path / "openspec" / "proposals").mkdir(parents=True, exist_ok=True)
    (repo_path / "openspec" / "proposals" / f"{fresh.id}.md").write_text(
        "---\nschema: girder.openspec/v1\ntitle: t\nintent: add the thing\n"
        "tasks:\n  - id: a\n    title: Do a\n    type: code_change\n"
        "    scope_globs: [\"src/**\"]\n    success_criteria: [\"a done\"]\n"
        "    depends_on: []\n---\nN.\n"
    )
    await _git(repo_path, "add", "-A")
    await _git(repo_path, "commit", "-m", "spec: freeze")
    await _git(repo_path, "update-ref", f"refs/heads/{fresh.branch}", "HEAD")
    # a real (non-empty) diff on the run branch, as a completed task would leave
    (repo_path / "src" / "lib.py").write_text(f'x = "{fresh.id[:8]}"\n')
    await _git(repo_path, "add", "-A")
    await _git(repo_path, "commit", "-m", "work")
    await _git(repo_path, "update-ref", f"refs/heads/{fresh.branch}", "HEAD")
    await _git(repo_path, "update-ref", "refs/heads/main", base)
    return fresh


async def _drive(d: DeliveryEngine, run_id: str, *, max_pumps: int = 12) -> str:
    """Pump the delivery engine until a terminal/parked status; return it."""
    for _ in range(max_pumps):
        run = await repo.get_run(d.db, run_id)
        assert run is not None
        await d.pump(run)
        fresh = await repo.get_run(d.db, run_id)
        assert fresh is not None
        if fresh.status in (
            RunStatus.MERGED,
            RunStatus.FAILED,
            RunStatus.ESCALATED,
            RunStatus.MERGE_PENDING_HUMAN,
        ):
            return fresh.status.value
    raise AssertionError("delivery pump did not converge")


# --------------------------------------------------------------------- T0


@pytest.mark.parametrize("dh", [0], indirect=True)
async def test_t0_parks_at_merge_pending_human_then_merges_on_click(dh: DHarness) -> None:
    gateway = FakeGateway(responses=[_resp(content=GOOD_VERDICT)])
    d = dh.delivery(gateway)
    assert await d.enter_delivery(dh.run) == "pr_opened"
    fresh = await repo.get_run(dh.db, dh.run.id)
    assert fresh is not None and fresh.status is RunStatus.PR_OPEN
    assert fresh.pr_number == 7

    status = await _drive(d, dh.run.id)
    assert status == "merge_pending_human"
    fresh = await repo.get_run(dh.db, dh.run.id)
    assert fresh is not None and fresh.status is RunStatus.MERGE_PENDING_HUMAN
    assert any("ready to merge" in c[1] for c in dh.notifier.calls)
    assert dh.api.merge_calls == 0  # T0 never merges by itself

    # human clicks merge out-of-band → pump observes and runs cleanup
    dh.api.pr_merged = True
    status = await _drive(d, dh.run.id)
    assert status == "merged"
    fresh = await repo.get_run(dh.db, dh.run.id)
    assert fresh is not None and fresh.status is RunStatus.MERGED
    project = await repo.get_project(dh.db, dh.project.id)
    assert project is not None and project.clean_merge_streak == 1
    event = await repo.get_latest_event(dh.db, dh.run.id, "run_merged")
    assert event is not None and event["payload"]["pr_number"] == 7


# --------------------------------------------------------------------- T1


@pytest.mark.parametrize("dh", [1], indirect=True)
async def test_t1_auto_merges_notifies_and_bumps_streak(dh: DHarness) -> None:
    gateway = FakeGateway(responses=[_resp(content=GOOD_VERDICT)])
    d = dh.delivery(gateway)
    assert await d.enter_delivery(dh.run) == "pr_opened"
    status = await _drive(d, dh.run.id)
    assert status == "merged"
    assert dh.api.merge_calls == 1
    fresh = await repo.get_run(dh.db, dh.run.id)
    assert fresh is not None and fresh.status is RunStatus.MERGED
    project = await repo.get_project(dh.db, dh.project.id)
    assert project is not None and project.clean_merge_streak == 1
    assert any("merged" in c[1].lower() for c in dh.notifier.calls)
    # conformance verdict was clean: no PR comment posted
    assert dh.api.comments == []


@pytest.mark.parametrize("dh", [1], indirect=True)
async def test_t1_review_window_full_parks_for_human(dh: DHarness) -> None:
    # two prior merged-but-unreviewed runs exceed the window of 3 after this one
    for i in range(3):
        other = await repo.create_run(dh.db, dh.project.id, f"old {i}", f"run/old{i}", 1.0)
        await seed_run_status(dh.db, other.id, "merged")
    gateway = FakeGateway(responses=[_resp(content=GOOD_VERDICT)])
    d = dh.delivery(gateway)
    assert await d.enter_delivery(dh.run) == "pr_opened"
    status = await _drive(d, dh.run.id)
    assert status == "merge_pending_human"
    assert dh.api.merge_calls == 0
    fresh = await repo.get_run(dh.db, dh.run.id)
    assert fresh is not None and fresh.status is RunStatus.MERGE_PENDING_HUMAN


# --------------------------------------------------------------------- T2


@pytest.mark.parametrize("dh", [2], indirect=True)
async def test_t2_without_earned_streak_is_gated(dh: DHarness) -> None:
    gateway = FakeGateway(responses=[_resp(content=GOOD_VERDICT)])
    d = dh.delivery(gateway)
    assert await d.enter_delivery(dh.run) == "pr_opened"
    status = await _drive(d, dh.run.id)
    assert status == "merge_pending_human"
    assert dh.api.merge_calls == 0

    # a second project earns the streak (default threshold 10) → merges silently
    project = await repo.create_project(dh.db, "p2", str(dh.repo_path), autonomy_tier=2)
    await dh.db.execute(
        "UPDATE projects SET clean_merge_streak = 10 WHERE id = ?", (project.id,)
    )
    await dh.db.conn.commit()
    fresh2 = await _new_run(dh.db, dh.repo_path, project, "run/dlv2", "second thing")
    gateway2 = FakeGateway(responses=[_resp(content=GOOD_VERDICT)])
    d2 = dh.delivery(gateway2)
    assert await d2.enter_delivery(fresh2) == "pr_opened"
    status = await _drive(d2, fresh2.id)
    assert status == "merged"
    assert any("ready to merge" not in c[1] for c in dh.notifier.calls)


@pytest.mark.parametrize("dh", [2], indirect=True)
async def test_t2_catastrophic_conformance_escalates_despite_streak(dh: DHarness) -> None:
    await dh.db.execute(
        "UPDATE projects SET clean_merge_streak = 25 WHERE id = ?", (dh.project.id,)
    )
    await dh.db.conn.commit()
    gateway = FakeGateway(responses=[_resp(content=CATASTROPHIC_VERDICT)])
    d = dh.delivery(gateway)
    assert await d.enter_delivery(dh.run) == "pr_opened"
    status = await _drive(d, dh.run.id)
    assert status == "escalated"
    assert dh.api.merge_calls == 0
    fresh = await repo.get_run(dh.db, dh.run.id)
    assert fresh is not None and fresh.status is RunStatus.ESCALATED
    assert any("catastrophic" in c[2] for c in dh.notifier.calls)


# ------------------------------------------------------------- CI failure


@pytest.mark.parametrize("dh", [1], indirect=True)
async def test_ci_lint_failure_diagnosed_and_fixed_then_merged(dh: DHarness) -> None:
    """SC-09 core: induced lint failure (no test ids ⇒ novel) → fix agent → green."""
    dh.api.checks = [
        check(
            "lint",
            "failure",
            summary="F401 unused import `os` in src/lib.py — ruff failed",
            text="src/lib.py:1:1: F401 `os` imported but unused",
        )
    ]
    # scripted turns: fix agent turns first, then the conformance verdict
    dh.sandbox = ScriptSandbox(suite_results=[], suite_xml=GREEN_XML)
    compliant = [
        _resp(calls=[_tc("1", "write_file",
                         '{"path":"src/lib.py","content":"x = 2  # fixed\\n"}')]),
        _resp(calls=[_tc("2", "run_command",
                         '{"cmd":"git add -A && git commit -m fix-lint"}')]),
        _resp(calls=[_tc("3", "mark_task_complete", '{"summary":"removed unused import"}')]),
    ]
    gateway = FakeGateway(responses=[*compliant, _resp(content=GOOD_VERDICT)])
    d = dh.delivery(gateway)
    assert await d.enter_delivery(dh.run) == "pr_opened"

    # first pump observes the red CI and parks in ci_fixing
    run = await repo.get_run(dh.db, dh.run.id)
    assert run is not None
    descriptor = await d.pump(run)
    assert descriptor == "ci_red_fixing"
    fresh = await repo.get_run(dh.db, dh.run.id)
    assert fresh is not None and fresh.status is RunStatus.CI_FIXING

    # failure excerpt persisted redacted for the audit trail
    rows = await repo.list_ci_check_results_for_run(dh.db, dh.run.id)
    assert rows and rows[0]["check_name"] == "lint"

    # next pump runs the diagnostic fix agent and re-polls (now green)
    dh.api.checks = [check("lint", "success")]
    descriptor = await d.pump(fresh)
    assert descriptor == "ci_fix_pushed"

    status = await _drive(d, dh.run.id)
    assert status == "merged"

    # exactly one fix task, typed FIX, merged through the normal verification
    tasks = await repo.list_tasks_for_run(dh.db, dh.run.id)
    fix_tasks = [t for t in tasks if t.title.startswith("fix:")]
    assert len(fix_tasks) == 1
    assert fix_tasks[0].task_type is TaskType.FIX
    events = await dh.db.fetchall(
        "SELECT event_type FROM agent_events WHERE run_id = ?", (dh.run.id,)
    )
    assert any(e["event_type"] == "ci_fix_attempted" for e in events)


@pytest.mark.parametrize("dh", [1], indirect=True)
async def test_ci_fix_cap_exhausted_fails_run_without_dispatch(dh: DHarness) -> None:
    """ci_fix_attempts already consumed ⇒ the next red-CI episode fails the run
    and the diagnostic agent is never even dispatched (D5 anti-thrash cap)."""
    dh.api.checks = [
        check("lint", "failure", summary="E501 line too long — ruff failed", text="")
    ]
    # two prior fix tasks ⇒ cap (ci_fix_attempts=2) already reached
    wave = await repo.get_or_create_wave0(dh.db, dh.run.id)
    for i in range(2):
        await repo.create_task(
            dh.db, wave.id, i + 1, f"fix: earlier ({i + 1})", TaskType.FIX,
            scope_globs=["**"], spec_slice_md="earlier failure",
        )
    gateway = FakeGateway(responses=[])
    d = dh.delivery(gateway)
    assert await d.enter_delivery(dh.run) == "pr_opened"

    run = await repo.get_run(dh.db, dh.run.id)
    assert run is not None
    assert await d.pump(run) == "ci_red_fixing"

    fresh = await repo.get_run(dh.db, dh.run.id)
    assert fresh is not None
    status = await _drive(d, dh.run.id)
    assert status == "failed"
    assert gateway.calls == []  # exhausted ⇒ zero model dispatch
    fresh = await repo.get_run(dh.db, dh.run.id)
    assert fresh is not None and fresh.status is RunStatus.FAILED


@pytest.mark.parametrize("dh", [1], indirect=True)
async def test_new_flake_regression_escalates_not_tagged_flaky(dh: DHarness) -> None:
    """SC-10 core: green-on-rerun but NOT flaky on baseline ⇒ regression ⇒ escalated."""
    dh.api.checks = [
        check(
            "tests",
            "failure",
            summary="FAILED tests/test_race.py::test_sometimes",
            text="tests/test_race.py::test_sometimes flaky locally",
        )
    ]
    gateway = FakeGateway(responses=[])
    d = dh.delivery(gateway)
    assert await d.enter_delivery(dh.run) == "pr_opened"
    status = await _drive(d, dh.run.id)
    # rerun is green (ScriptSandbox writes a green report) but baseline has no
    # such flake ⇒ regression, escalate — never silently tagged known-flaky.
    assert status == "escalated"
    fresh = await repo.get_run(dh.db, dh.run.id)
    assert fresh is not None and fresh.status is RunStatus.ESCALATED
    flakies = await repo.list_flaky_tests(dh.db, dh.project.id)
    assert flakies == []  # NOT tagged
    assert any("regression" in c[2].lower() for c in dh.notifier.calls)


# ------------------------------------------------------------- merge gate


@pytest.mark.parametrize("dh", [1], indirect=True)
async def test_integrity_violations_block_delivery_at_entry(dh: DHarness) -> None:
    """WP 4.5: the ledger blocks merge at every tier — before any push/PR."""
    await dh.db.execute(
        "UPDATE runs SET integrity_violations = 2 WHERE id = ?", (dh.run.id,)
    )
    await dh.db.conn.commit()
    d = dh.delivery(FakeGateway(responses=[]))
    descriptor = await d.enter_delivery(dh.run)
    assert descriptor == "failed"
    fresh = await repo.get_run(dh.db, dh.run.id)
    assert fresh is not None and fresh.status is RunStatus.FAILED
    assert dh.api.api_calls == []  # nothing pushed, no PR, no API call at all


@pytest.mark.parametrize("dh", [1], indirect=True)
async def test_red_delivery_suite_fails_run_before_push(dh: DHarness) -> None:
    dh.sandbox = ScriptSandbox(suite_results=[], suite_xml=RED_XML)
    d = dh.delivery(FakeGateway(responses=[]))
    descriptor = await d.enter_delivery(dh.run)
    assert descriptor == "failed"
    fresh = await repo.get_run(dh.db, dh.run.id)
    assert fresh is not None and fresh.status is RunStatus.FAILED
    event = await repo.get_latest_event(dh.db, dh.run.id, "delivery_suite")
    assert event is not None and event["payload"]["green"] is False
