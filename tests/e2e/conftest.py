"""Shared fixtures for the end-to-end scenario suite (impl-plan §12).

Everything here runs the REAL RunEngine/TaskEngine code paths against a real
temp git repo (the copied ``tests/fixtures/e2e-target`` tree) with
``LocalExecSandbox`` — baseline AND verify suites are REAL pytest runs on the
host, executed by the *running* interpreter (``sys.executable -m pytest``). The model is a scripted :class:`FakeGateway` (same idiom as
``tests/unit/test_task_engine.py``); SC-07 swaps in a real ``ModelGateway``
over an ``httpx.MockTransport``. No podman, no network.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
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
from girder.db.models import Project, Run, RunStatus
from girder.github.client import GitHubClient
from girder.guard.redact import Redactor
from girder.orchestrator.run_engine import RunEngine
from girder.sandbox.engine import ExecResult
from girder.sandbox.local import LocalExecSandbox
from girder.specs.freeze import approve_and_freeze
from girder.util import run_host_cmd
from tests.conftest import seed_run_status

FIXTURE_SRC = Path(__file__).resolve().parents[1] / "fixtures" / "e2e-target"
PLANTED_SECRET = "girder-hunter2-do-not-echo"
PLANTED_ENV_VAR = "GIRDER_PLANTED_SECRET"


async def git(cwd: Path, *args: str, check: bool = True) -> str:
    result = await run_host_cmd(["git", "-C", str(cwd), *args], check=check, timeout_s=30)
    return result.stdout


def _host_pytest_usable() -> bool:
    """The sandbox execs ``<python_bin> -m pytest``; python_bin is pinned to
    the *running* interpreter below, so this can only fail if pytest itself
    is not importable by the very interpreter running this suite — i.e. a
    broken install, not a bare host. The skip it guards is a last resort."""
    probe = subprocess.run(
        [sys.executable, "-m", "pytest", "--version"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return probe.returncode == 0




def _host_interpreter_argv(cmd: list[str]) -> list[str]:
    """Map a bare ``python``/``python3`` interpreter argv onto the running one.

    The container runner image ships its own ``python3``; on a venv-only host
    there is no global interpreter, so the local sandbox stand-in resolves the
    bare name to :data:`sys.executable` (the image's interpreter equivalent).
    Only an exact bare name is translated — paths and other argv are untouched.
    """
    if cmd and cmd[0] in ("python", "python3"):
        return [sys.executable, *cmd[1:]]
    return cmd


class HostSuiteSandbox(LocalExecSandbox):
    """LocalExecSandbox that also rewrites container-isms in argv.

    Two rewrites, both mirroring what the real runner image provides:

    * ``--flag=/workspace/...`` argv elements: the orchestrator's suite
      commands pass the junit report as a single such element, which the base
      rewrite (exact ``/workspace`` prefix) leaves untouched — the nested real
      pytest would then try to write the host root. The path inside such flags
      is mapped to the worktree, so the REAL pytest suite runs and its report
      lands in the worktree, exactly as in a container.
    * a bare ``python``/``python3`` argv head (e.g. ``write_file``'s
      ``python3 -c`` decode snippet) is resolved to the running interpreter —
      the image has one; a venv-only host does not.
    """

    async def exec(
        self, name: str, cmd: list[str], *, timeout_s: float = 120.0, user: str | None = None
    ) -> ExecResult:
        rewritten = _host_interpreter_argv(cmd)
        for i, arg in enumerate(rewritten):
            if "=/workspace/" in arg:
                flag, _, path = arg.partition("=")
                target = self._rewrite("/" + path.removeprefix("/"), self._specs[name])
                rewritten[i] = f"{flag}={target}"
        return await super().exec(name, rewritten, timeout_s=timeout_s, user=user)


@dataclass
class FakeNotifier:
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    async def notify(
        self, level: str, title: str, body: str, *, run_id: str | None = None
    ) -> None:
        self.calls.append((level, title, body))


def first_user_content(messages: list[Any]) -> str:
    """Content of the first user-role message (FakeGateway routing key)."""
    for m in messages:
        if getattr(m, "role", "") == "user":
            return getattr(m, "content", "") or ""
    return ""


@dataclass
class FakeGateway:
    """Scripted per-call responses; records each complete() message list.

    Sprint 5 additions (backward compatible): ``routes`` dispatches on a
    substring of the FIRST user message (insertion order wins, non-empty
    queue required), ``delays`` sleeps before answering a routed call, and
    ``call_times`` records ``asyncio`` loop time parallel to ``calls`` so
    tests can prove overlap (SC-14 concurrency).
    """

    responses: list[Any]
    calls: list[list[Any]] = field(default_factory=list)
    call_times: list[float] = field(default_factory=list)
    # substring of FIRST user msg -> queue:
    routes: dict[str, list[Any]] = field(default_factory=dict)
    # substring -> seconds to sleep before returning:
    delays: dict[str, float] = field(default_factory=dict)

    def role_config(self, role: str) -> Any:
        return type("RoleCfg", (), {"context_window": 200_000, "max_output_tokens": 4096})()

    async def complete(
        self,
        role: str,
        messages: list[Any],
        *,
        run_id: str,
        attempt_id: str | None = None,
        tools: Any = None,
        temperature: float | None = None,
    ) -> Any:
        self.calls.append(list(messages))
        self.call_times.append(asyncio.get_running_loop().time())
        content = first_user_content(messages)
        for key, queue in self.routes.items():  # insertion order
            if key in content and queue:
                if key in self.delays:
                    await asyncio.sleep(self.delays[key])
                return queue.pop(0)
        if not self.responses:
            head = content[:150].replace("\n", " ")
            raise AssertionError(
                f"FakeGateway exhausted: no scripted response for message "
                f"(role={role!r}, routed keys tried: {list(self.routes)}) starting {head!r}"
            )
        return self.responses.pop(0)


def tc(id_: str, name: str, arguments_json: str) -> Any:
    from girder.models.gateway import ModelToolCall

    return ModelToolCall(id=id_, name=name, arguments_json=arguments_json)


def resp(calls: list[Any] | None = None, content: str | None = None) -> Any:
    from girder.models.gateway import ModelResponse, Usage

    return ModelResponse(
        content=content,
        tool_calls=calls or [],
        finish_reason="tool_calls" if calls else "stop",
        usage=Usage(),
        role="tier2",
        model_id="fake",
        provider="fake",
    )


def write_resp(tc_id: str, path: str, content: str) -> Any:
    """One scripted model turn: a JSON-safe ``write_file`` tool call.

    The response carries a PLAN block (WP 8.1) so the planning phase unlocks
    in the same turn and the scripted write executes."""
    return resp(
        content=f"PLAN:\n- Write: {path}\n- Test: pytest",
        calls=[tc(tc_id, "write_file", json.dumps({"path": path, "content": content}))],
    )


COMMIT_TURN = resp(calls=[tc("c", "run_command", '{"cmd":"git add -A && git commit -m work"}')])


def write_commit_of(path: str, content: str, *, tc_id: str = "1") -> list[Any]:
    """A scripted attempt: write ``path``, commit everything, declare done."""
    return [
        write_resp(tc_id, path, content),
        resp(
            calls=[tc(f"{tc_id}-c", "run_command", '{"cmd":"git add -A && git commit -m work"}')]
        ),
        resp(calls=[tc(f"{tc_id}-d", "mark_task_complete", '{"summary":"done"}')]),
    ]


@dataclass
class E2eCtx:
    db: Database
    project: Project
    run: Run
    repo_path: Path
    settings: Settings
    redactor: Redactor
    notifier: FakeNotifier
    github: Any = None  # GitHubClient (delivery ctxs only)
    api: Any = None  # FakeGitHub transport (delivery ctxs only)

    def engine(self, gateway: Any, sandbox: LocalExecSandbox | None = None) -> RunEngine:
        return RunEngine(
            db=self.db,
            settings=self.settings,
            secrets=Secrets(models_openrouter_api_key="sk-test-nonsecret"),
            gateway=gateway,
            sandbox=sandbox or HostSuiteSandbox(),
            notifier=self.notifier,
            redactor=self.redactor,
            repo_path=self.repo_path,
        )

    def delivery_engine(self, gateway: Any, sandbox: LocalExecSandbox | None = None) -> RunEngine:
        """Full pipeline (spec → … → PR → CI → conformance → merge)."""
        assert self.github is not None, "delivery_engine requires a delivery ctx"
        return RunEngine(
            db=self.db,
            settings=self.settings,
            secrets=Secrets(models_openrouter_api_key="sk-test-nonsecret"),
            gateway=gateway,
            sandbox=sandbox or HostSuiteSandbox(),
            notifier=self.notifier,
            redactor=self.redactor,
            github=self.github,
            repo_path=self.repo_path,
        )


MakeCtx = Callable[..., Awaitable[E2eCtx]]


@pytest.fixture
def redactor() -> Redactor:
    return Redactor(secret_env_names=[PLANTED_ENV_VAR])


@pytest.fixture
def settings() -> Settings:
    return Settings(
        project=ProjectConfig(test_directories=["tests"]),
        limits=LimitsConfig(
            task_max_attempts=2,
            attempt_max_turns=8,
            attempt_wallclock_s=120,
            planning_turns=0,  # WP 8.1 phase is unit-tested; e2e scripts route on exact flow
        ),
        # Pin the suite interpreter to the one running this suite: the nested
        # baseline/verify pytest runs then work on any host regardless of what
        # `python`/`python3` on PATH point at (venv-only installs).
        sandbox=SandboxNetwork(python_bin=sys.executable),
    )


@pytest.fixture
async def e2e_repo(tmp_path: Path) -> AsyncIterator[Path]:
    """The fixture project copied to tmp and committed on a real main branch."""
    if not _host_pytest_usable():
        pytest.skip(
            "host pytest unavailable: sys.executable cannot run `python -m pytest` "
            "(broken environment — SC-01 sentinel not exercised)"
        )
    repo_path = tmp_path / "repo"
    shutil.copytree(FIXTURE_SRC, repo_path)
    await git(repo_path, "init", "-b", "main")
    await git(repo_path, "config", "user.email", "e2e@girder.local")
    await git(repo_path, "config", "user.name", "girder-e2e")
    await git(repo_path, "add", "-A")
    await git(repo_path, "commit", "-m", "initial: calculator without divide")
    yield repo_path


@pytest.fixture
async def make_ctx(
    db: Database,
    e2e_repo: Path,
    settings: Settings,
    redactor: Redactor,
    tmp_path: Path,
) -> AsyncIterator[MakeCtx]:
    """Factory: freeze a proposal for a fresh run and return its pump context."""

    async def _make(
        proposal_text: str,
        *,
        budget_cap_usd: float = 5.0,
        branch: str = "run/e2e1",
    ) -> E2eCtx:
        project = await repo.create_project(db, "e2e-target", str(e2e_repo))
        run = await repo.create_run(db, project.id, "e2e intent", branch, budget_cap_usd)
        await seed_run_status(db, run.id, RunStatus.SPEC_PENDING.value)
        fresh = await repo.get_run(db, run.id)
        assert fresh is not None
        await approve_and_freeze(
            db,
            project=project,
            run=fresh,
            proposal_text=proposal_text,
            repo_path=e2e_repo,
            worktree_base=tmp_path / "freeze-wt",
        )
        fresh = await repo.get_run(db, run.id)
        assert fresh is not None and fresh.status is RunStatus.SPEC_APPROVED
        return E2eCtx(
            db=db,
            project=project,
            run=fresh,
            repo_path=e2e_repo,
            settings=settings,
            redactor=redactor,
            notifier=FakeNotifier(),
        )

    yield _make


# ------------------------------------------------------------------ Sprint 4


GH_OWNER = "acme"
GH_NAME = "widget"
E2E_GITHUB_TOKEN = "github_pat_e2etoken1234567890ABCDEF"


def gh_check(
    name: str, conclusion: str, *, summary: str = "", text: str = ""
) -> dict[str, Any]:
    return {
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "html_url": f"https://github.com/{GH_OWNER}/{GH_NAME}/runs/1",
        "output": {"summary": summary, "text": text},
    }


class FakeGitHub(httpx.MockTransport):
    """The GitHub REST surface the delivery pump touches, in-process.

    Same semantics as the unit-suite copy in tests/unit/test_delivery.py;
    duplicated (not imported) so the suites stay decoupled. ``checks`` stays
    mutable after client construction: tests flip ``ctx.api.checks`` mid-run.
    """

    def __init__(
        self,
        checks: list[dict[str, Any]],
        *,
        pr_number: int = 7,
        pr_merged: bool = False,
    ) -> None:
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
        base = f"/repos/{GH_OWNER}/{GH_NAME}"
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


MakeDeliveryCtx = Callable[..., Awaitable[E2eCtx]]


@pytest.fixture
async def make_delivery_ctx(
    db: Database,
    e2e_repo: Path,
    settings: Settings,
    redactor: Redactor,
    tmp_path: Path,
) -> AsyncIterator[MakeDeliveryCtx]:
    """Delivery-capable factory: bare origin remote + FakeGitHub + client."""
    clients: list[Any] = []
    counter = 0

    async def _make(
        proposal_text: str,
        *,
        autonomy_tier: int = 1,
        clean_merge_streak: int | None = None,
        budget_cap_usd: float = 5.0,
        branch: str = "run/e2e1",
    ) -> E2eCtx:
        origin = tmp_path / "origin.git"
        await run_host_cmd(["git", "init", "--bare", "-b", "main", str(origin)], timeout_s=30)
        remotes = (await git(e2e_repo, "remote")).split()
        if "origin" in remotes:
            await git(e2e_repo, "remote", "set-url", "origin", str(origin))
        else:
            await git(e2e_repo, "remote", "add", "origin", str(origin))
        await git(e2e_repo, "push", "origin", "main")

        nonlocal counter
        counter += 1
        project = await repo.create_project(
            db, f"e2e-target-{counter}", str(e2e_repo), autonomy_tier=autonomy_tier
        )
        if clean_merge_streak is not None:
            await db.execute(
                "UPDATE projects SET clean_merge_streak = ? WHERE id = ?",
                (clean_merge_streak, project.id),
            )
            await db.conn.commit()
        run = await repo.create_run(db, project.id, "e2e intent", branch, budget_cap_usd)
        await seed_run_status(db, run.id, RunStatus.SPEC_PENDING.value)
        fresh = await repo.get_run(db, run.id)
        assert fresh is not None
        await approve_and_freeze(
            db,
            project=project,
            run=fresh,
            proposal_text=proposal_text,
            repo_path=e2e_repo,
            worktree_base=tmp_path / "freeze-wt",
        )
        fresh = await repo.get_run(db, run.id)
        assert fresh is not None and fresh.status is RunStatus.SPEC_APPROVED

        delivery_settings = settings.model_copy(
            update={"github": GithubConfig(poll_interval_s=0.0)}
        )
        api = FakeGitHub([gh_check("ci", "success")])
        github = GitHubClient(
            delivery_settings,
            Secrets(github_token=E2E_GITHUB_TOKEN),
            redactor,
            db,
            repo_path=e2e_repo,
            transport=api,
            owner_repo=(GH_OWNER, GH_NAME),  # remote is a local bare repo
        )
        clients.append(github)
        return E2eCtx(
            db=db,
            project=project,
            run=fresh,
            repo_path=e2e_repo,
            settings=delivery_settings,
            redactor=redactor,
            notifier=FakeNotifier(),
            github=github,
            api=api,
        )

    try:
        yield _make
    finally:
        for client in clients:
            await client.aclose()
